"""End-to-end: ingest (demo venue) -> Postgres/Redis -> API. Needs local Postgres + Redis."""

import os
from datetime import datetime, timezone

import redis as syncredis
from fastapi.testclient import TestClient

from predictarb import bus, db
from predictarb.config import Settings
from predictarb.ingest.worker import Ingestor
from predictarb.models import Quote
from predictarb.venues.demo import DemoAdapter


async def test_ingest_alerts_and_api(clean_db):
    settings = Settings(database_url=clean_db, redis_url=os.environ["REDIS_URL"], venues="demo")
    pool = await db.create_pool(clean_db)
    redis = bus.connect(settings.redis_url)
    adapter = DemoAdapter()
    ing = Ingestor(settings, pool, redis, [adapter])
    try:
        await db.upsert_venue(pool, "demo", adapter.venue_name)
        n = await ing.sync_catalog(adapter)
        assert n == len(adapter.rows) > 20
        assert await pool.fetchval("SELECT count(*) FROM market WHERE status='open'") == n
        assert await pool.fetchval("SELECT count(*) FROM price_tick") == n

        target = "demo:DEMO-001"
        cur = await pool.fetchval("SELECT mid FROM market WHERE id=$1", target)
        rule = await db.create_alert_rule(pool, target, "price_above", round(cur + 0.05, 4), cooldown_minutes=0)

        # Unchanged quotes are not re-written; a crossing quote fires the alert.
        async def quotes():
            return [Quote(venue="demo", venue_market_id="DEMO-001", yes_bid=cur + 0.09, yes_ask=cur + 0.11,
                          last_price=cur + 0.1, ts=datetime.now(timezone.utc))]
        adapter.fetch_quotes = quotes
        assert await ing.poll_quotes(adapter) == 1
        assert await ing.poll_quotes(adapter) == 0, "identical quote is skipped"
        notes = await db.list_notifications(pool)
        assert len(notes) == 1 and notes[0]["rule_id"] == rule["id"] and "rose to" in notes[0]["message"]
        assert await redis.xlen(bus.NOTIFICATIONS) == 1
        assert await redis.xlen(bus.QUOTES) == 1

        # Backfill writes history and marks the market.
        row = await db.get_market_row(pool, "demo:DEMO-002")
        assert await ing.backfill_one(row) == 30 * 24
        assert (await db.get_market_row(pool, "demo:DEMO-002"))["backfilled_at"] is not None

        # Catalog sync marks markets a venue stopped listing.
        adapter.rows = adapter.rows[1:]
        await ing.sync_catalog(adapter)
        assert await pool.fetchval("SELECT status FROM market WHERE id=$1", target) == "unlisted"
    finally:
        await pool.close()
        await redis.aclose()

    from predictarb.api.main import app

    with TestClient(app) as client:
        meta = client.get("/api/meta").json()
        assert meta["read_only"] is True and meta["venues"][0]["id"] == "demo"
        assert sum(meta["category_counts"].values()) == n - 1

        page = client.get("/api/markets", params={"limit": 5, "sort": "volume"}).json()
        assert page["total"] == n - 1 and len(page["items"]) == 5
        vols = [i["volume_24h_usd"] for i in page["items"]]
        assert vols == sorted(vols, reverse=True)
        assert "rules_text" not in page["items"][0] and "sparkline" in page["items"][0]

        crypto = client.get("/api/markets", params={"category": "Crypto"}).json()
        assert crypto["total"] > 0 and all(i["category"] == "Crypto" for i in crypto["items"])
        search = client.get("/api/markets", params={"q": "Bitcoin"}).json()
        assert search["total"] > 0 and all("Bitcoin" in i["title"] for i in search["items"])
        await_pool_mid = client.get("/api/markets", params={"limit": 200}).json()["items"]
        decided = [i for i in await_pool_mid if i["mid"] is not None and not 0.03 <= i["mid"] <= 0.97]
        contested = client.get("/api/markets", params={"limit": 200, "contested": "true"}).json()
        assert contested["total"] == page["total"] - len(decided)
        assert all(i["mid"] is None or 0.03 <= i["mid"] <= 0.97 for i in contested["items"])
        assert sum(client.get("/api/meta", params={"contested": "true"}).json()["category_counts"].values()) == contested["total"]
        for s in db.SORTS:
            assert client.get("/api/markets", params={"sort": s}).status_code == 200

        detail = client.get("/api/markets/demo:DEMO-002").json()
        assert detail["rules_text"].startswith("DEMO market")
        hist = client.get("/api/markets/demo:DEMO-002/history", params={"range": "1W"}).json()
        assert len(hist["points"]) > 100 and hist["backfill_requested"] is False
        hist3 = client.get("/api/markets/demo:DEMO-003/history").json()
        assert hist3["backfill_requested"] is True
        assert client.get("/api/markets/nope").status_code == 404

        assert client.put("/api/watchlist/demo:DEMO-003", json={"notes": "watch me"}).json()["ok"]
        assert client.put("/api/watchlist/demo:NOPE").status_code == 404
        r = client.post("/api/alerts", json={"market_id": "demo:DEMO-004", "kind": "move", "threshold": 0.05})
        assert r.status_code == 201
        assert client.post("/api/alerts", json={"market_id": "demo:DEMO-004", "kind": "move", "threshold": 2}).status_code == 422
        wl = client.get("/api/watchlist").json()["items"]
        assert {i["id"] for i in wl} == {"demo:DEMO-003", "demo:DEMO-004"}
        assert next(i for i in wl if i["id"] == "demo:DEMO-004")["alert_rules"][0]["kind"] == "move"
        assert client.get("/api/markets", params={"q": "DEMO"}).status_code == 200

        notes = client.get("/api/notifications").json()
        assert notes["unread"] == 1
        client.post("/api/notifications/read")
        assert client.get("/api/notifications").json()["unread"] == 0

        client.delete(f"/api/alerts/{r.json()['id']}")
        client.delete("/api/watchlist/demo:DEMO-003")
        assert {i["id"] for i in client.get("/api/watchlist").json()["items"]} == {"demo:DEMO-004"}

        # Live feed: a bus event reaches the browser socket.
        with client.websocket_connect("/ws") as ws:
            rs = syncredis.from_url(os.environ["REDIS_URL"])
            import time

            time.sleep(0.5)  # relay is blocking on XREAD '$'
            # Back-to-back events on two streams must both arrive (regression: XREAD with '$').
            rs.xadd(bus.QUOTES, {"data": bus.dumps([{"id": "demo:DEMO-002", "mid": 0.5}])})
            rs.xadd(bus.NOTIFICATIONS, {"data": bus.dumps({"id": 99, "message": "hi"})})
            rs.xadd(bus.QUOTES, {"data": bus.dumps([{"id": "demo:DEMO-002", "mid": 0.6}])})
            got = [ws.receive_json() for _ in range(3)]
            assert {"type": "quotes", "data": [{"id": "demo:DEMO-002", "mid": 0.5}]} in got
            assert {"type": "notification", "data": {"id": 99, "message": "hi"}} in got
            assert {"type": "quotes", "data": [{"id": "demo:DEMO-002", "mid": 0.6}]} in got
            rs.close()
