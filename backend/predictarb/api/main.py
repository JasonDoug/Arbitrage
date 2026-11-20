"""HTTP + WebSocket API for the web app (design doc section 2, service 9)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from datetime import datetime, timezone

from predictarb import arb, bus, db
from predictarb.categories import CATEGORIES
from predictarb.config import get_settings
from predictarb.migrate import migrate

log = logging.getLogger("predictarb.api")


class Hub:
    """Fans bus events out to connected browsers."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def broadcast(self, message: dict[str, Any]) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


async def _relay(app: FastAPI) -> None:
    last: dict[str, str] = {}
    while True:
        if not last:
            try:
                last = {s: await bus.latest_id(app.state.redis, s) for s in (bus.QUOTES, bus.NOTIFICATIONS)}
            except Exception as e:
                log.warning("bus relay init error: %s", e)
                await asyncio.sleep(3)
                continue
        try:
            msgs = await bus.read(app.state.redis, dict(last), block_ms=5000)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("bus relay error: %s", e)
            await asyncio.sleep(3)
            continue
        for stream, msg_id, payload in msgs:
            last[stream] = msg_id
            kind = "quotes" if stream == bus.QUOTES else "notification"
            await app.state.hub.broadcast({"type": kind, "data": payload})


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    await migrate(s.database_url)
    app.state.pool = await db.create_pool(s.database_url)
    app.state.redis = bus.connect(s.redis_url)
    app.state.hub = Hub()
    relay = asyncio.create_task(_relay(app))
    try:
        yield
    finally:
        relay.cancel()
        await asyncio.gather(relay, return_exceptions=True)
        await app.state.pool.close()
        await app.state.redis.aclose()


app = FastAPI(title="predictarb", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().cors_origins.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _pool():
    return app.state.pool


# ---------------------------------------------------------------- meta

@app.get("/api/health")
async def health() -> dict[str, Any]:
    await _pool().fetchval("SELECT 1")
    await app.state.redis.ping()
    return {"ok": True}


@app.get("/api/meta")
async def meta(contested: bool = False) -> dict[str, Any]:
    return {
        "mode": "live",
        "read_only": True,
        "categories": CATEGORIES,
        "category_counts": await db.category_counts(_pool(), contested),
        "venues": await db.list_venues(_pool()),
        "sorts": list(db.SORTS),
    }


# ---------------------------------------------------------------- markets

@app.get("/api/markets")
async def markets(
    category: str | None = None,
    venue: str | None = None,
    q: str | None = Query(None, max_length=200),
    sort: str = "volume",
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    spark: bool = True,
    contested: bool = False,
) -> dict[str, Any]:
    rows, total = await db.list_markets(
        _pool(), category=category, venue=venue, q=q, sort=sort, limit=limit, offset=offset, contested=contested
    )
    if spark:
        lines = await db.sparklines(_pool(), [r["id"] for r in rows])
        for r in rows:
            r["sparkline"] = lines.get(r["id"], [])
    for r in rows:
        r.pop("rules_text", None)  # fetched on detail
    return {"total": total, "items": rows}


@app.get("/api/markets/{market_id}")
async def market_detail(market_id: str) -> dict[str, Any]:
    row = await db.get_market_row(_pool(), market_id)
    if row is None:
        raise HTTPException(404, "market not found")
    watch = await _pool().fetchrow("SELECT * FROM watchlist_item WHERE user_id='me' AND market_id=$1", market_id)
    row["watched"] = watch is not None
    row["alert_rules"] = await db.list_alert_rules(_pool(), market_id)
    return row


@app.get("/api/markets/{market_id}/history")
async def market_history(market_id: str, range: Literal["1D", "1W", "1M", "ALL"] = "1W") -> dict[str, Any]:
    row = await db.get_market_row(_pool(), market_id)
    if row is None:
        raise HTTPException(404, "market not found")
    points = await db.price_history(_pool(), market_id, range)
    backfill_requested = False
    if row.get("backfilled_at") is None:
        await bus.publish(app.state.redis, bus.BACKFILL_REQUESTS, {"market_id": market_id})
        backfill_requested = True
    return {"market_id": market_id, "range": range, "points": points, "backfill_requested": backfill_requested}


# ---------------------------------------------------------------- watchlist

class WatchIn(BaseModel):
    notes: str | None = Field(None, max_length=1000)


@app.get("/api/watchlist")
async def watchlist() -> dict[str, Any]:
    items = await db.list_watchlist(_pool())
    lines = await db.sparklines(_pool(), [r["id"] for r in items])
    rules = await db.list_alert_rules(_pool())
    by_market: dict[str, list[dict[str, Any]]] = {}
    for r in rules:
        by_market.setdefault(r["market_id"], []).append(r)
    for it in items:
        it["sparkline"] = lines.get(it["id"], [])
        it["alert_rules"] = by_market.get(it["id"], [])
        it.pop("rules_text", None)
    return {"items": items}


@app.put("/api/watchlist/{market_id}")
async def watch(market_id: str, body: WatchIn | None = None) -> dict[str, Any]:
    if not await db.add_watch(_pool(), market_id, body.notes if body else None):
        raise HTTPException(404, "market not found")
    return {"ok": True}


@app.delete("/api/watchlist/{market_id}")
async def unwatch(market_id: str) -> dict[str, Any]:
    await db.remove_watch(_pool(), market_id)
    return {"ok": True}


# ---------------------------------------------------------------- alerts & notifications

class AlertIn(BaseModel):
    market_id: str
    kind: Literal["price_above", "price_below", "move"]
    threshold: float = Field(..., gt=0, lt=1, description="probability (0-1); for 'move' the absolute change")
    window_minutes: int = Field(60, ge=1, le=10080)
    cooldown_minutes: int = Field(60, ge=0, le=10080)


@app.get("/api/alerts")
async def list_alerts(market_id: str | None = None) -> dict[str, Any]:
    return {"items": await db.list_alert_rules(_pool(), market_id)}


@app.post("/api/alerts", status_code=201)
async def create_alert(body: AlertIn) -> dict[str, Any]:
    if await db.get_market_row(_pool(), body.market_id) is None:
        raise HTTPException(404, "market not found")
    await db.add_watch(_pool(), body.market_id)  # alerting implies watching
    return await db.create_alert_rule(
        _pool(), body.market_id, body.kind, body.threshold, body.window_minutes, body.cooldown_minutes
    )


@app.delete("/api/alerts/{rule_id}")
async def delete_alert(rule_id: int) -> dict[str, Any]:
    await db.delete_alert_rule(_pool(), rule_id)
    return {"ok": True}


@app.get("/api/notifications")
async def notifications(unread: bool = False) -> dict[str, Any]:
    items = await db.list_notifications(_pool(), unread_only=unread)
    unread_count = await _pool().fetchval("SELECT count(*) FROM notification WHERE user_id='me' AND NOT read")
    return {"items": items, "unread": unread_count}


@app.post("/api/notifications/read")
async def notifications_read() -> dict[str, Any]:
    await db.mark_notifications_read(_pool())
    return {"ok": True}


# ---------------------------------------------------------------- cross-venue links & arbitrage

class LinkReview(BaseModel):
    status: Literal["confirmed", "rejected", "suggested"]
    relation: Literal["identical", "inverse"] | None = None
    note: str | None = Field(None, max_length=1000)


@app.get("/api/links")
async def links(
    status: Literal["suggested", "confirmed", "rejected"] | None = "suggested",
    q: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    items, total = await db.list_links(_pool(), status, limit, offset, q)
    return {"items": items, "total": total, "counts": await db.link_counts(_pool())}


@app.patch("/api/links/{link_id}")
async def review(link_id: int, body: LinkReview) -> dict[str, Any]:
    r = await db.review_link(_pool(), link_id, body.status, body.relation, body.note)
    if r is None:
        raise HTTPException(404, "link not found")
    return r


_SIDE_KEYS = ("id", "venue", "title", "outcome_label", "url", "category", "yes_bid", "yes_ask", "mid", "quote_ts", "close_ts")


@app.get("/api/arb")
async def arbitrage(
    include_unconfirmed: bool = False,
    min_edge: float = Query(0.0, ge=-1, le=1, description="minimum net edge per contract, in dollars"),
    sort: Literal["edge", "annualized"] = "edge",
) -> dict[str, Any]:
    s = get_settings()
    now = datetime.now(timezone.utc)
    rows = await db.links_for_arb(_pool(), include_suggested=include_unconfirmed)
    found = arb.scan(rows, now, s.arb_max_quote_age_s, s.polymarket_fee_rates)
    episodes = await db.arb_episodes(_pool())
    items = []
    for link, opp in found:
        if opp.edge < min_edge:
            continue
        ep = episodes.get((link["id"], opp.direction))
        items.append(
            {
                **opp.as_dict(),
                "link": {k: link[k] for k in ("id", "status", "relation", "score", "rules_changed")},
                "a": {k: link["a"][k] for k in _SIDE_KEYS},
                "b": {k: link["b"][k] for k in _SIDE_KEYS},
                "first_seen": ep["first_seen"] if ep else None,
            }
        )
    key = (lambda i: i["edge"]) if sort == "edge" else (lambda i: i["annualized"] if i["annualized"] is not None else -9)
    items.sort(key=key, reverse=True)
    fresh_links = {l["id"] for l, _ in found}
    return {
        "items": items,
        "links_considered": len(rows),
        "links_with_stale_quotes": len(rows) - len(fresh_links),
        "max_quote_age_s": s.arb_max_quote_age_s,
    }


# ---------------------------------------------------------------- live feed

@app.websocket("/ws")
async def ws_feed(ws: WebSocket) -> None:
    await ws.accept()
    app.state.hub.clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # keepalive pings from the client; content ignored
    except WebSocketDisconnect:
        pass
    finally:
        app.state.hub.clients.discard(ws)


def run() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = get_settings()
    uvicorn.run("predictarb.api.main:app", host=s.api_host, port=s.api_port, proxy_headers=True)


if __name__ == "__main__":
    run()
