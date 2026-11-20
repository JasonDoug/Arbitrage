"""Database access. All SQL lives here."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from predictarb.models import Market, PricePoint, Quote

SORTS = {
    "volume": "m.volume_24h_usd DESC NULLS LAST",
    "change": "abs(m.change_24h) DESC NULLS LAST",
    "closing": "m.close_ts ASC NULLS LAST",
    "spread": "m.spread ASC NULLS LAST",
    "liquidity": "m.liquidity_usd DESC NULLS LAST",
    "newest": "m.first_seen DESC",
}

MARKET_COLUMNS = """
    id, venue, venue_market_id, event_id, event_title, title, outcome_label, url, category, tags,
    rules_text, resolution_source, close_ts, status, volume_unit, total_volume, extra,
    yes_bid, yes_ask, last_price, mid, spread, change_24h, volume_24h, volume_24h_usd,
    open_interest, liquidity_usd, quote_ts, first_seen, last_seen
"""


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str, **kw: Any) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, init=_init_conn, min_size=1, max_size=kw.pop("max_size", 10), **kw)


def _row(r: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


# ---------------------------------------------------------------- venues

async def upsert_venue(pool: asyncpg.Pool, venue_id: str, name: str) -> None:
    await pool.execute(
        "INSERT INTO venue (id, name) VALUES ($1, $2) ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",
        venue_id,
        name,
    )


async def set_venue_status(pool: asyncpg.Pool, venue_id: str, ok: bool, error: str | None = None) -> None:
    if ok:
        await pool.execute(
            "UPDATE venue SET status='ok', last_ok_at=now(), last_error=NULL WHERE id=$1", venue_id
        )
    else:
        await pool.execute(
            "UPDATE venue SET status='error', last_error=$2 WHERE id=$1", venue_id, (error or "")[:500]
        )


async def list_venues(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """SELECT v.*, (SELECT count(*) FROM market m WHERE m.venue = v.id AND m.status = 'open') AS open_markets
           FROM venue v ORDER BY v.id"""
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- catalog

_UPSERT_MARKET = f"""
INSERT INTO market (
    id, venue, venue_market_id, event_id, event_title, title, outcome_label, url, category, tags,
    rules_text, rules_hash, resolution_source, close_ts, status, volume_unit, total_volume, extra,
    last_seen
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18, now())
ON CONFLICT (id) DO UPDATE SET
    event_id = EXCLUDED.event_id,
    event_title = EXCLUDED.event_title,
    title = EXCLUDED.title,
    outcome_label = EXCLUDED.outcome_label,
    url = EXCLUDED.url,
    category = EXCLUDED.category,
    tags = EXCLUDED.tags,
    rules_text = EXCLUDED.rules_text,
    rules_hash = EXCLUDED.rules_hash,
    resolution_source = EXCLUDED.resolution_source,
    close_ts = EXCLUDED.close_ts,
    status = EXCLUDED.status,
    volume_unit = EXCLUDED.volume_unit,
    total_volume = EXCLUDED.total_volume,
    extra = EXCLUDED.extra,
    last_seen = now()
RETURNING (xmax = 0) AS inserted
"""


async def upsert_markets(pool: asyncpg.Pool, markets: list[Market]) -> int:
    """Upsert catalog rows (and their quotes, if present). Returns number of new markets."""
    inserted = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for m in markets:
                r = await conn.fetchrow(
                    _UPSERT_MARKET,
                    m.id, m.venue, m.venue_market_id, m.event_id, m.event_title, m.title,
                    m.outcome_label, m.url, m.category, m.tags, m.rules_text, m.rules_hash,
                    m.resolution_source, m.close_ts, m.status, m.volume_unit, m.total_volume, m.extra,
                )
                inserted += 1 if r and r["inserted"] else 0
    quotes = [m.quote for m in markets if m.quote is not None]
    if quotes:
        await apply_quotes(pool, quotes)
    return inserted


async def mark_unseen_closed(pool: asyncpg.Pool, venue: str, since: datetime) -> int:
    """Markets a venue stopped listing during a full catalog sync are no longer open."""
    res = await pool.execute(
        "UPDATE market SET status='unlisted' WHERE venue=$1 AND status='open' AND last_seen < $2",
        venue,
        since,
    )
    return int(res.split()[-1])


_UPDATE_QUOTE = """
UPDATE market SET
    yes_bid=$2, yes_ask=$3, last_price=$4, mid=$5, spread=$6,
    change_24h=COALESCE($7, change_24h),
    volume_24h=COALESCE($8, volume_24h),
    volume_24h_usd=COALESCE($9, volume_24h_usd),
    open_interest=COALESCE($10, open_interest),
    liquidity_usd=COALESCE($11, liquidity_usd),
    quote_ts=$12
WHERE id=$1
"""


async def apply_quotes(pool: asyncpg.Pool, quotes: list[Quote], record_ticks: bool = True) -> None:
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                _UPDATE_QUOTE,
                [
                    (
                        q.id, q.yes_bid, q.yes_ask, q.last_price, q.mid, q.spread, q.change_24h,
                        q.volume_24h, q.volume_24h_usd, q.open_interest, q.liquidity_usd, q.ts or now,
                    )
                    for q in quotes
                ],
            )
            if record_ticks:
                ticks = [
                    (q.id, q.ts or now, q.mid, q.yes_bid, q.yes_ask, q.volume_24h)
                    for q in quotes
                    if q.mid is not None
                ]
                if ticks:
                    await conn.executemany(
                        """INSERT INTO price_tick (market_id, ts, price, yes_bid, yes_ask, volume, source)
                           SELECT $1, $2, $3, $4, $5, $6, 'poll'
                           WHERE EXISTS (SELECT 1 FROM market WHERE id = $1)
                           ON CONFLICT DO NOTHING""",
                        ticks,
                    )


async def insert_history(pool: asyncpg.Pool, market_id: str, points: list[PricePoint]) -> int:
    if not points:
        await pool.execute("UPDATE market SET backfilled_at=now() WHERE id=$1", market_id)
        return 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """INSERT INTO price_tick (market_id, ts, price, yes_bid, yes_ask, volume, source)
                   VALUES ($1, $2, $3, $4, $5, $6, 'backfill') ON CONFLICT DO NOTHING""",
                [(market_id, p.ts, p.price, p.yes_bid, p.yes_ask, p.volume) for p in points],
            )
            await conn.execute("UPDATE market SET backfilled_at=now() WHERE id=$1", market_id)
    return len(points)


async def markets_needing_backfill(pool: asyncpg.Pool, top_n: int) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        f"""
        (SELECT {MARKET_COLUMNS} FROM market
          WHERE status='open' AND backfilled_at IS NULL
          ORDER BY volume_24h_usd DESC NULLS LAST LIMIT $1)
        UNION
        (SELECT {', '.join('m.' + c.strip() for c in MARKET_COLUMNS.split(','))}
           FROM market m JOIN watchlist_item w ON w.market_id = m.id
          WHERE m.backfilled_at IS NULL)
        """,
        top_n,
    )
    return [dict(r) for r in rows]


async def get_market_row(pool: asyncpg.Pool, market_id: str) -> dict[str, Any] | None:
    return _row(await pool.fetchrow(f"SELECT {MARKET_COLUMNS}, backfilled_at FROM market WHERE id=$1", market_id))


# ---------------------------------------------------------------- queries for the API

async def list_markets(
    pool: asyncpg.Pool,
    *,
    category: str | None = None,
    venue: str | None = None,
    q: str | None = None,
    sort: str = "volume",
    status: str = "open",
    limit: int = 60,
    offset: int = 0,
    user_id: str = "me",
) -> tuple[list[dict[str, Any]], int]:
    where = ["m.status = $1"]
    args: list[Any] = [status]
    if category:
        args.append(category)
        where.append(f"m.category = ${len(args)}")
    if venue:
        args.append(venue)
        where.append(f"m.venue = ${len(args)}")
    if q:
        args.append(q)
        where.append(
            f"(m.search @@ websearch_to_tsquery('english', ${len(args)}) OR m.title ILIKE '%' || ${len(args)} || '%')"
        )
    order = SORTS.get(sort, SORTS["volume"])
    where_sql = " AND ".join(where)
    total = await pool.fetchval(f"SELECT count(*) FROM market m WHERE {where_sql}", *args)
    args.extend([user_id, limit, offset])
    cols = ", ".join("m." + c.strip() for c in MARKET_COLUMNS.split(","))
    rows = await pool.fetch(
        f"""SELECT {cols}, (w.market_id IS NOT NULL) AS watched
            FROM market m
            LEFT JOIN watchlist_item w ON w.market_id = m.id AND w.user_id = ${len(args) - 2}
            WHERE {where_sql}
            ORDER BY {order}, m.id
            LIMIT ${len(args) - 1} OFFSET ${len(args)}""",
        *args,
    )
    return [dict(r) for r in rows], int(total)


async def category_counts(pool: asyncpg.Pool, venue: str | None = None) -> dict[str, int]:
    if venue:
        rows = await pool.fetch(
            "SELECT category, count(*) n FROM market WHERE status='open' AND venue=$1 GROUP BY category", venue
        )
    else:
        rows = await pool.fetch("SELECT category, count(*) n FROM market WHERE status='open' GROUP BY category")
    return {r["category"]: r["n"] for r in rows}


RANGES = {
    "1D": (timedelta(days=1), timedelta(minutes=5)),
    "1W": (timedelta(days=7), timedelta(hours=1)),
    "1M": (timedelta(days=30), timedelta(hours=4)),
    "ALL": (timedelta(days=3650), timedelta(days=1)),
}


async def price_history(pool: asyncpg.Pool, market_id: str, rng: str = "1W") -> list[dict[str, Any]]:
    span, bucket = RANGES.get(rng, RANGES["1W"])
    rows = await pool.fetch(
        """SELECT date_bin($3::interval, ts, TIMESTAMPTZ '2000-01-01') AS t,
                  (array_agg(price ORDER BY ts DESC))[1] AS close,
                  max(price) AS high, min(price) AS low,
                  (array_agg(price ORDER BY ts ASC))[1] AS open
           FROM price_tick
           WHERE market_id = $1 AND ts >= now() - $2::interval
           GROUP BY 1 ORDER BY 1""",
        market_id,
        span,
        bucket,
    )
    return [dict(r) for r in rows]


async def sparklines(pool: asyncpg.Pool, market_ids: list[str], points: int = 24) -> dict[str, list[float]]:
    """Hourly closes over the last 24h for a batch of markets."""
    if not market_ids:
        return {}
    rows = await pool.fetch(
        """SELECT market_id, date_bin('1 hour', ts, TIMESTAMPTZ '2000-01-01') AS t,
                  (array_agg(price ORDER BY ts DESC))[1] AS close
           FROM price_tick
           WHERE market_id = ANY($1) AND ts >= now() - interval '24 hours'
           GROUP BY 1, 2 ORDER BY 1, 2""",
        market_ids,
    )
    out: dict[str, list[float]] = {}
    for r in rows:
        out.setdefault(r["market_id"], []).append(round(r["close"], 4))
    return {k: v[-points:] for k, v in out.items()}


# ---------------------------------------------------------------- watchlist & alerts

async def list_watchlist(pool: asyncpg.Pool, user_id: str = "me") -> list[dict[str, Any]]:
    cols = ", ".join("m." + c.strip() for c in MARKET_COLUMNS.split(","))
    rows = await pool.fetch(
        f"""SELECT {cols}, w.added_at, w.added_price, w.notes, TRUE AS watched
            FROM watchlist_item w JOIN market m ON m.id = w.market_id
            WHERE w.user_id = $1 ORDER BY w.added_at DESC""",
        user_id,
    )
    return [dict(r) for r in rows]


async def add_watch(pool: asyncpg.Pool, market_id: str, notes: str | None = None, user_id: str = "me") -> bool:
    res = await pool.execute(
        """INSERT INTO watchlist_item (user_id, market_id, added_price, notes)
           SELECT $1, id, mid, $3 FROM market WHERE id = $2
           ON CONFLICT (user_id, market_id) DO UPDATE SET notes = COALESCE(EXCLUDED.notes, watchlist_item.notes)""",
        user_id,
        market_id,
        notes,
    )
    return res.endswith("1")


async def remove_watch(pool: asyncpg.Pool, market_id: str, user_id: str = "me") -> None:
    await pool.execute("DELETE FROM watchlist_item WHERE user_id=$1 AND market_id=$2", user_id, market_id)


async def list_alert_rules(pool: asyncpg.Pool, market_id: str | None = None, user_id: str = "me") -> list[dict[str, Any]]:
    if market_id:
        rows = await pool.fetch(
            "SELECT * FROM alert_rule WHERE user_id=$1 AND market_id=$2 ORDER BY id", user_id, market_id
        )
    else:
        rows = await pool.fetch("SELECT * FROM alert_rule WHERE user_id=$1 ORDER BY id", user_id)
    return [dict(r) for r in rows]


async def create_alert_rule(
    pool: asyncpg.Pool,
    market_id: str,
    kind: str,
    threshold: float,
    window_minutes: int = 60,
    cooldown_minutes: int = 60,
    user_id: str = "me",
) -> dict[str, Any]:
    r = await pool.fetchrow(
        """INSERT INTO alert_rule (user_id, market_id, kind, threshold, window_minutes, cooldown_minutes)
           VALUES ($1,$2,$3,$4,$5,$6) RETURNING *""",
        user_id, market_id, kind, threshold, window_minutes, cooldown_minutes,
    )
    return dict(r)


async def delete_alert_rule(pool: asyncpg.Pool, rule_id: int, user_id: str = "me") -> None:
    await pool.execute("DELETE FROM alert_rule WHERE id=$1 AND user_id=$2", rule_id, user_id)


async def enabled_rules_by_market(pool: asyncpg.Pool) -> dict[str, list[dict[str, Any]]]:
    rows = await pool.fetch("SELECT * FROM alert_rule WHERE enabled")
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["market_id"], []).append(dict(r))
    return out


async def price_at(pool: asyncpg.Pool, market_id: str, at: datetime) -> float | None:
    return await pool.fetchval(
        "SELECT price FROM price_tick WHERE market_id=$1 AND ts <= $2 ORDER BY ts DESC LIMIT 1", market_id, at
    )


async def fire_rule(pool: asyncpg.Pool, rule: dict[str, Any], message: str) -> dict[str, Any]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE alert_rule SET last_fired_at=now() WHERE id=$1", rule["id"])
            r = await conn.fetchrow(
                """INSERT INTO notification (user_id, market_id, rule_id, kind, message)
                   VALUES ($1,$2,$3,$4,$5) RETURNING *""",
                rule["user_id"], rule["market_id"], rule["id"], rule["kind"], message,
            )
    return dict(r)


async def list_notifications(pool: asyncpg.Pool, unread_only: bool = False, limit: int = 50, user_id: str = "me") -> list[dict[str, Any]]:
    rows = await pool.fetch(
        f"""SELECT n.*, m.title AS market_title FROM notification n LEFT JOIN market m ON m.id = n.market_id
            WHERE n.user_id=$1 {'AND NOT n.read' if unread_only else ''}
            ORDER BY n.ts DESC LIMIT $2""",
        user_id,
        limit,
    )
    return [dict(r) for r in rows]


async def mark_notifications_read(pool: asyncpg.Pool, user_id: str = "me") -> None:
    await pool.execute("UPDATE notification SET read=TRUE WHERE user_id=$1 AND NOT read", user_id)
