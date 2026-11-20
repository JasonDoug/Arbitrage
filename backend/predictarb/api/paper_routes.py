"""Paper trading and order-book endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from predictarb import arb, books, db, paper
from predictarb.config import get_settings

router = APIRouter()


def _pool(request: Request):
    return request.app.state.pool


def _fetcher(request: Request) -> books.BookFetcher:
    return request.app.state.fetcher


async def _market(request: Request, market_id: str) -> dict[str, Any]:
    m = await db.get_market_row(_pool(request), market_id)
    if m is None:
        raise HTTPException(404, "market not found")
    return m


@router.get("/api/markets/{market_id}/book")
async def market_book(request: Request, market_id: str, depth: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
    book = await _fetcher(request).get(await _market(request, market_id))
    return book.as_dict(depth)


# ---------------------------------------------------------------- accounts


class AccountIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    starting_cash: float = Field(10_000, gt=0, le=10_000_000)


async def _summary(request: Request, acct: dict[str, Any], mark: str = "mid") -> dict[str, Any]:
    _, fills, orders = await paper.account_state(_pool(request), acct["id"])
    mkts = await db.markets_by_ids(_pool(request), list({f["market_id"] for f in fills}))
    return paper.summarize(acct, fills, mkts, orders, mark)


@router.get("/api/paper/accounts")
async def accounts(request: Request) -> dict[str, Any]:
    out = []
    for a in await db.paper_accounts(_pool(request)):
        s = await _summary(request, a)
        out.append({**a, "equity": s["equity"], "total_pnl": s["total_pnl"], "return_pct": s["return_pct"]})
    return {"items": out}


@router.post("/api/paper/accounts", status_code=201)
async def create_account(request: Request, body: AccountIn) -> dict[str, Any]:
    r = await _pool(request).fetchrow(
        "INSERT INTO paper_account (name, starting_cash) VALUES ($1, $2) ON CONFLICT DO NOTHING RETURNING *",
        body.name.strip(), body.starting_cash,
    )
    if r is None:
        raise HTTPException(409, "an account with that name exists")
    return dict(r)


@router.post("/api/paper/accounts/{account_id}/reset")
async def reset_account(request: Request, account_id: int, starting_cash: float | None = Query(None, gt=0)) -> dict[str, Any]:
    pool = _pool(request)
    async with pool.acquire() as conn:
        async with conn.transaction():
            r = await conn.fetchrow(
                "UPDATE paper_account SET reset_at=now(), starting_cash=COALESCE($2, starting_cash) WHERE id=$1 RETURNING *",
                account_id, starting_cash,
            )
            if r is None:
                raise HTTPException(404, "account not found")
            for t in ("paper_fill", "paper_order", "paper_equity"):
                await conn.execute(f"DELETE FROM {t} WHERE account_id=$1", account_id)
    return dict(r)


# ---------------------------------------------------------------- orders


class OrderIn(BaseModel):
    account_id: int
    market_id: str
    side: Literal["YES", "NO"]
    action: Literal["buy", "sell"]
    type: Literal["market", "limit"] = "market"
    qty: float = Field(..., gt=0, le=paper.MAX_QTY)
    limit_price: float | None = Field(None, gt=0, lt=1)


def _req(body: OrderIn, group: str | None = None) -> paper.OrderRequest:
    return paper.OrderRequest(body.account_id, body.market_id, body.side, body.action, body.type,
                              round(body.qty, 2), body.limit_price, group)


@router.post("/api/paper/preview")
async def preview(request: Request, body: OrderIn) -> dict[str, Any]:
    m = await _market(request, body.market_id)
    try:
        return {"ok": True, **await paper.preview(_pool(request), _fetcher(request), _req(body), m, get_settings().polymarket_fee_rates)}
    except paper.OrderRejected as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/paper/orders", status_code=201)
async def place_order(request: Request, body: OrderIn) -> dict[str, Any]:
    m = await _market(request, body.market_id)
    try:
        return await paper.place(_pool(request), _fetcher(request), _req(body), m, get_settings().polymarket_fee_rates)
    except paper.OrderRejected as e:
        raise HTTPException(422, str(e))


@router.delete("/api/paper/orders/{order_id}")
async def cancel_order(request: Request, order_id: int, account_id: int) -> dict[str, Any]:
    if not await paper.cancel(_pool(request), account_id, order_id):
        raise HTTPException(404, "no open order with that id")
    return {"ok": True}


@router.get("/api/paper/portfolio")
async def portfolio(request: Request, account_id: int, mark: Literal["mid", "exit"] = "mid") -> dict[str, Any]:
    pool = _pool(request)
    acct = await pool.fetchrow("SELECT * FROM paper_account WHERE id=$1", account_id)
    if acct is None:
        raise HTTPException(404, "account not found")
    summary = await _summary(request, dict(acct), mark)
    orders = await pool.fetch(
        """SELECT o.*, m.title, m.outcome_label, m.venue FROM paper_order o JOIN market m ON m.id = o.market_id
           WHERE o.account_id=$1 ORDER BY o.created_at DESC LIMIT 200""",
        account_id,
    )
    fills = await pool.fetch(
        """SELECT f.*, m.title, m.outcome_label, m.venue, o.group_id FROM paper_fill f
           JOIN market m ON m.id = f.market_id LEFT JOIN paper_order o ON o.id = f.order_id
           WHERE f.account_id=$1 ORDER BY f.ts DESC LIMIT 200""",
        account_id,
    )
    equity = await pool.fetch("SELECT ts, equity FROM paper_equity WHERE account_id=$1 ORDER BY ts", account_id)
    curve = [dict(r) for r in equity] + [{"ts": datetime.now(timezone.utc), "equity": summary["equity"]}]
    return {
        "account": dict(acct),
        "summary": summary,
        "max_drawdown_pct": paper.max_drawdown([c["equity"] for c in curve]),
        "open_orders": [dict(o) for o in orders if o["status"] == "open"],
        "orders": [dict(o) for o in orders],
        "fills": [dict(f) for f in fills],
        "equity_curve": curve,
    }


# ---------------------------------------------------------------- arbitrage depth & execution


async def leg_ladders(request: Request, link: dict[str, Any], opp: arb.Opportunity) -> tuple[list, list, str]:
    fetch = _fetcher(request)
    book_a = await fetch.get(link["a"])
    book_b = await fetch.get(link["b"])
    a, b = opp.legs
    src = "book" if book_a.source == book_b.source == "book" else "partial (top of book)"
    return book_a.asks(a.side), book_b.asks(b.side), src


class ArbIn(BaseModel):
    account_id: int
    link_id: int
    direction: str
    qty: float = Field(..., gt=0, le=paper.MAX_QTY)


@router.post("/api/paper/arb", status_code=201)
async def paper_arb(request: Request, body: ArbIn) -> dict[str, Any]:
    """Buy both legs of an arbitrage as market orders (leg A first, then B) in one group."""
    pool = _pool(request)
    links = [l for l in await db.links_for_arb(pool, include_suggested=True) if l["id"] == body.link_id]
    if not links:
        raise HTTPException(404, "link not found or a market is closed")
    link = links[0]
    opps = {o.direction: o for o in arb.evaluate_link(link, link["a"], link["b"])}
    opp = opps.get(body.direction)
    if opp is None:
        raise HTTPException(422, "that direction has no executable quotes right now")
    group = paper.new_group()
    results = []
    s = get_settings()
    for leg, side_market in zip(opp.legs, (link["a"], link["b"])):
        m = await _market(request, side_market["id"])
        req = paper.OrderRequest(body.account_id, m["id"], leg.side, "buy", "limit", round(body.qty, 2),
                                 min(0.99, round(leg.price + 0.02, 4)), group)
        try:
            r = await paper.place(pool, _fetcher(request), req, m, s.polymarket_fee_rates)
            if r["status"] == "open" and r["rest_qty"] > 0:  # don't leave half a hedge resting
                await paper.cancel(pool, body.account_id, r["order_id"])
                r["status"] = "partial" if r["fill_qty"] else "cancelled"
            results.append({"market_id": m["id"], "side": leg.side, **r})
        except paper.OrderRejected as e:
            results.append({"market_id": m["id"], "side": leg.side, "status": "rejected", "error": str(e)})
            break
    filled = [r.get("fill_qty", 0) for r in results]
    warning = None
    if len(results) < 2 or len(set(filled)) > 1:
        warning = "Legs filled unevenly: the position is not fully hedged. Check the Portfolio tab."
    return {"group_id": group, "legs": results, "warning": warning}
