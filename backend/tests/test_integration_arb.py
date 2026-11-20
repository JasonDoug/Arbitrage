"""Matcher -> review -> arbitrage, through Postgres and the API."""

import os
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from predictarb import bus, db
from predictarb.config import Settings
from predictarb.ingest.worker import Ingestor
from predictarb.models import Market, Quote

NOW = datetime.now(timezone.utc)


def mk(venue, vid, title, bid, ask, cat="Crypto", outcome=None):
    return Market(
        venue=venue, venue_market_id=vid, title=title, outcome_label=outcome, category=cat,
        close_ts=NOW + timedelta(days=30), rules_text=f"rules for {vid}",
        quote=Quote(venue=venue, venue_market_id=vid, yes_bid=bid, yes_ask=ask, last_price=ask, ts=NOW),
    )


async def test_match_review_and_arb(clean_db):
    settings = Settings(database_url=clean_db, redis_url=os.environ["REDIS_URL"], venues="kalshi,polymarket", match_min_score=0.3)
    pool = await db.create_pool(clean_db)
    redis = bus.connect(settings.redis_url)
    ing = Ingestor(settings, pool, redis, [])
    try:
        await db.upsert_venue(pool, "kalshi", "Kalshi")
        await db.upsert_venue(pool, "polymarket", "Polymarket")
        await db.upsert_markets(pool, [
            mk("kalshi", "KXBTC-120", "Bitcoin price on Dec 31, 2026?", 0.38, 0.40, outcome="$120,000 or above"),
            mk("kalshi", "KXFED-H", "Fed decision in December 2026", 0.60, 0.62, "Economics", outcome="Hold"),
        ])
        await db.upsert_markets(pool, [
            mk("polymarket", "111", "Will Bitcoin be above $120k on December 31, 2026?", 0.62, 0.64),
            mk("polymarket", "222", "Wizards vs. Bulls", 0.40, 0.42, "Sports"),
        ])
        assert await ing.run_matcher() == 1
        assert await ing.run_matcher() == 0, "existing pairs aren't re-suggested"
        assert await db.kept_ids(pool, "polymarket") == frozenset({"111"}), "linked markets stay tracked"
    finally:
        await pool.close()
        await redis.aclose()

    from predictarb.api.main import app

    with TestClient(app) as client:
        res = client.get("/api/links").json()
        assert res["total"] == 1 and res["counts"] == {"suggested": 1}
        link = res["items"][0]
        assert (link["a"]["id"], link["b"]["id"]) == ("kalshi:KXBTC-120", "polymarket:111")
        assert link["rules_changed"] is False and link["a"]["rules_text"] == "rules for KXBTC-120"

        assert client.get("/api/arb").json()["items"] == [], "unconfirmed links are hidden by default"
        unconf = client.get("/api/arb", params={"include_unconfirmed": "true"}).json()["items"]
        assert len(unconf) == 1 and unconf[0]["link"]["status"] == "suggested"

        assert client.patch(f"/api/links/{link['id']}", json={"status": "confirmed", "relation": "identical"}).status_code == 200
        assert client.patch("/api/links/999999", json={"status": "confirmed"}).status_code == 404
        opps = client.get("/api/arb").json()
        assert len(opps["items"]) == 1, "min_edge=0 hides the losing direction"
        o = opps["items"][0]
        assert o["direction"] == "yes_a_no_b" and o["edge"] > 0.15
        assert [(l["side"], l["price"]) for l in o["legs"]] == [("YES", 0.4), ("NO", 0.38)]
        assert o["first_seen"] is None, "no episode until the worker records one"
        both = client.get("/api/arb", params={"min_edge": -1}).json()["items"]
        assert len(both) == 2

    pool = await db.create_pool(clean_db)
    redis = bus.connect(settings.redis_url)
    ing = Ingestor(settings, pool, redis, [])
    try:
        assert await ing.scan_arb() == 1
        ep = await db.arb_episodes(pool)
        assert list(ep) and ep[next(iter(ep))]["best_edge"] > 0.15
        # Prices converge: the episode closes.
        await db.apply_quotes(pool, [Quote(venue="polymarket", venue_market_id="111", yes_bid=0.39, yes_ask=0.41,
                                           ts=datetime.now(timezone.utc))])
        assert await ing.scan_arb() == 0
        assert await db.arb_episodes(pool) == {}
        assert await pool.fetchval("SELECT count(*) FROM arb_opportunity WHERE closed_at IS NOT NULL") == 1
    finally:
        await pool.close()
        await redis.aclose()
