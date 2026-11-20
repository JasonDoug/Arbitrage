"""Paper trading (design doc 10, Mode A): real books, fake money.

Fill model:
- Marketable orders walk the venue's live order book at submission and pay the venue's
  taker fee. Whatever can't fill within the limit (or the fat-finger guard for market
  orders) is cancelled (market) or rests (limit).
- Resting limit orders fill later, at the limit price with no fee, only when a quote
  update shows the opposite side reached the limit. There is no queue position, so this is
  optimistic for orders sitting at the touch; limits strictly through the market are not.
- Settlement pays $1 per winning contract when the venue reports a result.

Positions, cost basis (average cost, fees included), cash and P&L are always derived from
the fills ledger.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import asyncpg

from predictarb import books

FAT_FINGER = 0.10  # market orders never fill more than 10c through the best price
MAX_QTY = 100_000


class OrderRejected(ValueError):
    pass


# ---------------------------------------------------------------- ledger math


@dataclass
class Position:
    market_id: str
    side: str
    qty: float = 0.0
    avg_cost: float = 0.0  # per contract, fees included
    realized: float = 0.0
    fees: float = 0.0
    opened_at: datetime | None = None


@dataclass
class Ledger:
    cash: float
    positions: dict[tuple[str, str], Position] = field(default_factory=dict)
    realized: float = 0.0
    fees: float = 0.0
    closed_trades: list[float] = field(default_factory=list)  # realized P&L of fully closed positions


def replay(starting_cash: float, fills: list[dict[str, Any]]) -> Ledger:
    """Rebuild cash and positions from fills in time order."""
    led = Ledger(cash=starting_cash)
    for f in sorted(fills, key=lambda x: (x["ts"], x["id"])):
        key = (f["market_id"], f["side"])
        pos = led.positions.setdefault(key, Position(f["market_id"], f["side"]))
        q, p, fee = f["qty"], f["price"], f["fee"] or 0.0
        led.fees += fee
        pos.fees += fee
        if f["action"] == "buy":
            led.cash -= p * q + fee
            new_qty = pos.qty + q
            pos.avg_cost = (pos.avg_cost * pos.qty + p * q + fee) / new_qty
            pos.qty = new_qty
            pos.opened_at = pos.opened_at or f["ts"]
        else:  # sell or settle
            led.cash += p * q - fee
            pnl = (p - pos.avg_cost) * q - fee
            pos.realized += pnl
            led.realized += pnl
            pos.qty = round(pos.qty - q, 6)
            if pos.qty <= 1e-9:
                led.closed_trades.append(pos.realized)
                pos.qty = 0.0
    return led


def marks(m: dict[str, Any], side: str) -> tuple[float | None, float | None]:
    """(exit price you could sell at now, mid) for a side."""
    if side == "YES":
        return m.get("yes_bid"), m.get("mid")
    exit_ = (1 - m["yes_ask"]) if m.get("yes_ask") else None
    mid = (1 - m["mid"]) if m.get("mid") is not None else None
    return exit_, mid


def summarize(account: dict[str, Any], fills: list[dict[str, Any]], markets: dict[str, dict[str, Any]],
              open_orders: list[dict[str, Any]], mark: str = "mid") -> dict[str, Any]:
    led = replay(account["starting_cash"], fills)
    rows, value = [], 0.0
    for (mid_, side), pos in led.positions.items():
        if pos.qty <= 0:
            continue
        m = markets.get(mid_, {})
        exit_p, mid_p = marks(m, side)
        px = (mid_p if mark == "mid" else exit_p)
        if m.get("result"):
            px = 1.0 if m["result"].upper() == side else 0.0
        mv = (px or 0.0) * pos.qty
        value += mv
        rows.append({
            "market_id": mid_, "side": side, "qty": round(pos.qty, 4), "avg_cost": round(pos.avg_cost, 4),
            "mark": px, "exit_price": exit_p, "value": round(mv, 2),
            "unrealized": round(((px or 0.0) - pos.avg_cost) * pos.qty, 2) if px is not None else None,
            "realized": round(pos.realized, 2), "opened_at": pos.opened_at,
            "title": m.get("title"), "outcome_label": m.get("outcome_label"), "venue": m.get("venue"),
            "close_ts": m.get("close_ts"), "status": m.get("status"), "result": m.get("result"),
        })
    reserved = sum((o["qty"] - o["filled_qty"]) * (o["limit_price"] or 0) for o in open_orders if o["action"] == "buy")
    equity = led.cash + value
    wins = [p for p in led.closed_trades if p > 0]
    return {
        "cash": round(led.cash, 2),
        "available_cash": round(led.cash - reserved, 2),
        "reserved_cash": round(reserved, 2),
        "positions_value": round(value, 2),
        "equity": round(equity, 2),
        "starting_cash": account["starting_cash"],
        "total_pnl": round(equity - account["starting_cash"], 2),
        "return_pct": round((equity / account["starting_cash"] - 1) * 100, 2) if account["starting_cash"] else None,
        "realized": round(led.realized, 2),
        "unrealized": round(sum(r["unrealized"] or 0 for r in rows), 2),
        "fees": round(led.fees, 2),
        "closed_positions": len(led.closed_trades),
        "win_rate": round(len(wins) / len(led.closed_trades) * 100, 1) if led.closed_trades else None,
        "positions": sorted(rows, key=lambda r: -(r["value"] or 0)),
    }


def max_drawdown(equity: list[float]) -> float | None:
    peak, worst = None, 0.0
    for e in equity:
        peak = e if peak is None else max(peak, e)
        if peak:
            worst = min(worst, e / peak - 1)
    return round(worst * 100, 2) if equity else None


# ---------------------------------------------------------------- order execution


@dataclass
class OrderRequest:
    account_id: int
    market_id: str
    side: str  # YES | NO
    action: str  # buy | sell
    type: str  # market | limit
    qty: float
    limit_price: float | None = None
    group_id: str | None = None


def plan(req: OrderRequest, m: dict[str, Any], book: books.Book, held: float, available_cash: float,
         pm_rates: dict[str, float] | None = None) -> dict[str, Any]:
    """Work out what an order would do right now. Pure: used for preview and execution."""
    if req.side not in ("YES", "NO") or req.action not in ("buy", "sell") or req.type not in ("market", "limit"):
        raise OrderRejected("invalid side/action/type")
    if not (0 < req.qty <= MAX_QTY):
        raise OrderRejected(f"quantity must be between 0 and {MAX_QTY:,}")
    if req.type == "limit" and not (req.limit_price and 0 < req.limit_price < 1):
        raise OrderRejected("limit price must be between 0 and 1")
    if m.get("status") != "open":
        raise OrderRejected("market is not open")
    buying = req.action == "buy"
    if not buying and req.qty > held + 1e-9:
        raise OrderRejected(f"you hold {held:g} {req.side}; can't sell {req.qty:g} (buy the other side to go short)")
    ladder = book.asks(req.side) if buying else book.bids(req.side)
    limit = req.limit_price
    if req.type == "market":
        if not ladder:
            raise OrderRejected("no liquidity on that side of the book")
        best = ladder[0][0]
        limit = min(0.99, best + FAT_FINGER) if buying else max(0.01, best - FAT_FINGER)
    fill = books.walk(ladder, req.qty, limit, buying)
    fee = books.taker_fee(m["venue"], fill.levels, m.get("category") or "Other", pm_rates)
    rest = round(req.qty - fill.qty, 4) if req.type == "limit" else 0.0
    cash_needed = (fill.cost + fee + rest * (limit or 0)) if buying else 0.0
    if buying and cash_needed > available_cash + 1e-6:
        raise OrderRejected(f"needs ${cash_needed:,.2f}, available ${available_cash:,.2f}")
    if req.type == "market" and fill.qty == 0:
        raise OrderRejected("nothing fillable within 10c of the best price")
    avg = fill.avg_price if fill.qty else None
    payout = (req.qty if buying else 0.0)
    return {
        "fill_qty": fill.qty,
        "avg_price": avg,
        "levels": fill.levels,
        "fee": fee,
        "cost": round(fill.cost + fee, 4) if buying else None,
        "proceeds": round(fill.cost - fee, 4) if not buying else None,
        "rest_qty": rest,
        "unfilled_cancelled": round(req.qty - fill.qty, 4) if req.type == "market" else 0.0,
        "limit_used": limit,
        "max_loss": round(fill.cost + fee + rest * (limit or 0), 2) if buying else None,
        "max_payout": round(payout, 2) if buying else None,
        "position_after": round(held + req.qty, 4) if buying else round(held - fill.qty, 4),
        "book_source": book.source,
    }


async def account_state(pool: asyncpg.Pool, account_id: int) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    acct = await pool.fetchrow("SELECT * FROM paper_account WHERE id=$1", account_id)
    if acct is None:
        raise OrderRejected("paper account not found")
    fills = [dict(r) for r in await pool.fetch("SELECT * FROM paper_fill WHERE account_id=$1", account_id)]
    orders = [dict(r) for r in await pool.fetch("SELECT * FROM paper_order WHERE account_id=$1 AND status='open'", account_id)]
    return dict(acct), fills, orders


def held_qty(fills: list[dict[str, Any]], market_id: str, side: str, open_orders: list[dict[str, Any]]) -> float:
    """Position size minus contracts already committed to resting sell orders."""
    q = 0.0
    for f in fills:
        if f["market_id"] == market_id and f["side"] == side:
            q += f["qty"] if f["action"] == "buy" else -f["qty"]
    committed = sum(o["qty"] - o["filled_qty"] for o in open_orders
                    if o["market_id"] == market_id and o["side"] == side and o["action"] == "sell")
    return round(q - committed, 6)


async def preview(pool, fetcher: books.BookFetcher, req: OrderRequest, m: dict[str, Any], pm_rates=None) -> dict[str, Any]:
    acct, fills, orders = await account_state(pool, req.account_id)
    led = summarize(acct, fills, {}, orders)
    book = await fetcher.get(m)
    return plan(req, m, book, held_qty(fills, req.market_id, req.side, orders), led["available_cash"], pm_rates)


async def place(pool, fetcher: books.BookFetcher, req: OrderRequest, m: dict[str, Any], pm_rates=None) -> dict[str, Any]:
    acct, fills, orders = await account_state(pool, req.account_id)
    led = summarize(acct, fills, {}, orders)
    book = await fetcher.get(m)
    p = plan(req, m, book, held_qty(fills, req.market_id, req.side, orders), led["available_cash"], pm_rates)
    status = "filled" if p["fill_qty"] >= req.qty - 1e-9 else ("open" if req.type == "limit" else "partial")
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        async with conn.transaction():
            oid = await conn.fetchval(
                """INSERT INTO paper_order (account_id, market_id, side, action, type, qty, limit_price, filled_qty,
                                            avg_price, fees, status, group_id, book_source, reason)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) RETURNING id""",
                req.account_id, req.market_id, req.side, req.action, req.type, req.qty, req.limit_price,
                p["fill_qty"], p["avg_price"], p["fee"], status, req.group_id, p["book_source"],
                f"{p['unfilled_cancelled']:g} unfilled, cancelled" if p["unfilled_cancelled"] > 0 else None,
            )
            if p["fill_qty"] > 0:
                # One fill row per order; the fee is charged on that order (Kalshi rounds per order).
                await conn.execute(
                    """INSERT INTO paper_fill (order_id, account_id, market_id, side, action, qty, price, fee, liquidity, ts)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'taker',$9)""",
                    oid, req.account_id, req.market_id, req.side, req.action, p["fill_qty"], p["avg_price"], p["fee"], now,
                )
    return {"order_id": oid, "status": status, **p}


async def cancel(pool, account_id: int, order_id: int) -> bool:
    res = await pool.execute(
        "UPDATE paper_order SET status='cancelled', updated_at=now() WHERE id=$1 AND account_id=$2 AND status='open'",
        order_id, account_id,
    )
    return res.endswith("1")


# ---------------------------------------------------------------- resting limits & settlement


def crossing(order: dict[str, Any], m: dict[str, Any]) -> float | None:
    """Available size if a resting limit order is now fillable from the latest quote, else None.
    Unknown sizes (Polymarket quotes) count as enough to fill the rest."""
    lim = order["limit_price"]
    if order["action"] == "buy":
        if order["side"] == "YES":
            ok, size = m.get("yes_ask") is not None and m["yes_ask"] <= lim, m.get("yes_ask_size")
        else:
            ok, size = m.get("yes_bid") is not None and 1 - m["yes_bid"] <= lim, m.get("yes_bid_size")
    else:
        if order["side"] == "YES":
            ok, size = m.get("yes_bid") is not None and m["yes_bid"] >= lim, m.get("yes_bid_size")
        else:
            ok, size = m.get("yes_ask") is not None and 1 - m["yes_ask"] >= lim, m.get("yes_ask_size")
    if not ok:
        return None
    return size if size else float("inf")


async def fill_resting(pool, m: dict[str, Any]) -> list[dict[str, Any]]:
    """Fill open limit orders on one market that the latest quote reaches. Returns fills."""
    done = []
    orders = [dict(r) for r in await pool.fetch(
        "SELECT * FROM paper_order WHERE market_id=$1 AND status='open' AND type='limit' ORDER BY created_at", m["id"])]
    for o in orders:
        avail = crossing(o, m)
        if avail is None:
            continue
        q = round(min(o["qty"] - o["filled_qty"], avail), 4)
        if q <= 0:
            continue
        if o["action"] == "sell":
            _, fills, open_orders = await account_state(pool, o["account_id"])
            others = [x for x in open_orders if x["id"] != o["id"]]
            q = min(q, max(0.0, held_qty(fills, o["market_id"], o["side"], others)))
            if q <= 0:
                continue
        new_filled = o["filled_qty"] + q
        avg = ((o["avg_price"] or 0) * o["filled_qty"] + o["limit_price"] * q) / new_filled
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO paper_fill (order_id, account_id, market_id, side, action, qty, price, fee, liquidity)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,0,'maker')""",
                    o["id"], o["account_id"], o["market_id"], o["side"], o["action"], q, o["limit_price"],
                )
                await conn.execute(
                    "UPDATE paper_order SET filled_qty=$2, avg_price=$3, status=$4, updated_at=now() WHERE id=$1",
                    o["id"], new_filled, avg, "filled" if new_filled >= o["qty"] - 1e-9 else "open",
                )
        done.append({"order_id": o["id"], "qty": q, "price": o["limit_price"], "market_id": m["id"], "side": o["side"], "action": o["action"]})
    return done


async def settle_market(pool, market_id: str, result: str) -> int:
    """Book a settlement fill (price 1 or 0) for every open paper position in a resolved market."""
    result = result.upper()
    rows = await pool.fetch(
        """SELECT account_id, side, sum(CASE WHEN action='buy' THEN qty ELSE -qty END) AS q
           FROM paper_fill WHERE market_id=$1 GROUP BY account_id, side""",
        market_id,
    )
    n = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE market SET result=$2, resolved_at=now() WHERE id=$1", market_id, result.lower())
            await conn.execute(
                "UPDATE paper_order SET status='cancelled', reason='market resolved', updated_at=now() WHERE market_id=$1 AND status='open'",
                market_id,
            )
            for r in rows:
                if r["q"] and r["q"] > 1e-9:
                    await conn.execute(
                        """INSERT INTO paper_fill (account_id, market_id, side, action, qty, price, fee, liquidity)
                           VALUES ($1,$2,$3,'settle',$4,$5,0,'settlement')""",
                        r["account_id"], market_id, r["side"], r["q"], 1.0 if r["side"] == result else 0.0,
                    )
                    n += 1
    return n


def new_group() -> str:
    return "arb-" + uuid.uuid4().hex[:10]
