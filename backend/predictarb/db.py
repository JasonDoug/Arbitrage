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
    # Upcoming end dates first; markets past their end date (still trading, awaiting
    # resolution) go after them, then markets with no end date.
    "closing": "(m.close_ts < now()) ASC NULLS LAST, m.close_ts ASC NULLS LAST",
    "spread": "m.spread ASC NULLS LAST",
    "liquidity": "m.liquidity_usd DESC NULLS LAST",
    "newest": "m.first_seen DESC",
}

CONTESTED_LOW, CONTESTED_HIGH = 0.03, 0.97

MARKET_COLUMNS = """
    id, venue, venue_market_id, event_id, event_title, title, outcome_label, url, category, tags,
    rules_text, resolution_source, close_ts, status, volume_unit, total_volume, extra,
    yes_bid, yes_ask, last_price, mid, spread, change_24h, volume_24h, volume_24h_usd,
    open_interest, liquidity_usd, quote_ts, first_seen, last_seen
"""


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


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
    contested: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    where = ["m.status = $1"]
    args: list[Any] = [status]
    if contested:
        # Hide markets the crowd already treats as decided (<3c or >97c); keep unpriced ones.
        where.append(f"(m.mid IS NULL OR m.mid BETWEEN {CONTESTED_LOW} AND {CONTESTED_HIGH})")
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


async def category_counts(pool: asyncpg.Pool, contested: bool = False) -> dict[str, int]:
    extra = f" AND (mid IS NULL OR mid BETWEEN {CONTESTED_LOW} AND {CONTESTED_HIGH})" if contested else ""
    rows = await pool.fetch(f"SELECT category, count(*) n FROM market WHERE status='open'{extra} GROUP BY category")
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


# ---------------------------------------------------------------- cross-venue links

LINK_COLUMNS = """
    l.id, l.market_a, l.market_b, l.status, l.relation, l.score, l.reasons, l.llm_relation,
    l.llm_confidence, l.llm_differences, l.llm_model, l.note, l.created_at, l.updated_at, l.reviewed_at,
    (l.rules_hash_a IS DISTINCT FROM a.rules_hash OR l.rules_hash_b IS DISTINCT FROM b.rules_hash) AS rules_changed
"""

_SIDE = """
    {p}.id AS {p}_id, {p}.venue AS {p}_venue, {p}.title AS {p}_title, {p}.outcome_label AS {p}_outcome_label,
    {p}.event_title AS {p}_event_title, {p}.category AS {p}_category, {p}.url AS {p}_url,
    {p}.close_ts AS {p}_close_ts, {p}.status AS {p}_status, {p}.yes_bid AS {p}_yes_bid, {p}.yes_ask AS {p}_yes_ask,
    {p}.mid AS {p}_mid, {p}.quote_ts AS {p}_quote_ts, {p}.rules_text AS {p}_rules_text,
    {p}.resolution_source AS {p}_resolution_source, {p}.volume_24h_usd AS {p}_volume_24h_usd
"""


def _split_link(r: asyncpg.Record) -> dict[str, Any]:
    d = dict(r)
    out = {k: v for k, v in d.items() if not (k.startswith("a_") or k.startswith("b_"))}
    for side in ("a", "b"):
        out[side] = {k[2:]: v for k, v in d.items() if k.startswith(side + "_")}
    return out


async def matcher_inputs(pool: asyncpg.Pool) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[tuple[str, str]]]:
    cols = "id, title, outcome_label, event_title, category, close_ts, rules_hash"
    a = await pool.fetch(f"SELECT {cols} FROM market WHERE status='open' AND venue='kalshi'")
    b = await pool.fetch(f"SELECT {cols} FROM market WHERE status='open' AND venue='polymarket'")
    existing = await pool.fetch("SELECT market_a, market_b FROM market_link")
    return [dict(r) for r in a], [dict(r) for r in b], {(r["market_a"], r["market_b"]) for r in existing}


async def insert_suggestions(pool: asyncpg.Pool, suggestions: list[Any]) -> int:
    """Insert new suggested links; existing pairs (in any status) are left alone."""
    n = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for s in suggestions:
                res = await conn.execute(
                    """INSERT INTO market_link (market_a, market_b, score, reasons, rules_hash_a, rules_hash_b)
                       SELECT $1, $2, $3, $4, a.rules_hash, b.rules_hash FROM market a, market b
                       WHERE a.id = $1 AND b.id = $2
                       ON CONFLICT (market_a, market_b) DO NOTHING""",
                    s.a, s.b, s.score, s.reasons,
                )
                n += int(res.split()[-1])
    return n


async def list_links(
    pool: asyncpg.Pool, status: str | None = "suggested", limit: int = 100, offset: int = 0, q: str | None = None
) -> tuple[list[dict[str, Any]], int]:
    where = ["a.status = 'open'", "b.status = 'open'"]
    args: list[Any] = []
    if status:
        args.append(status)
        where.append(f"l.status = ${len(args)}")
    if q:
        args.append(q)
        where.append(f"(a.title ILIKE '%' || ${len(args)} || '%' OR b.title ILIKE '%' || ${len(args)} || '%')")
    w = " AND ".join(where)
    base = "FROM market_link l JOIN market a ON a.id = l.market_a JOIN market b ON b.id = l.market_b"
    total = await pool.fetchval(f"SELECT count(*) {base} WHERE {w}", *args)
    args.extend([limit, offset])
    rows = await pool.fetch(
        f"SELECT {LINK_COLUMNS}, {_SIDE.format(p='a')}, {_SIDE.format(p='b')} {base} WHERE {w} "
        f"ORDER BY l.score DESC, l.id LIMIT ${len(args) - 1} OFFSET ${len(args)}",
        *args,
    )
    return [_split_link(r) for r in rows], int(total)


async def link_counts(pool: asyncpg.Pool) -> dict[str, int]:
    rows = await pool.fetch(
        """SELECT l.status, count(*) n FROM market_link l
           JOIN market a ON a.id = l.market_a JOIN market b ON b.id = l.market_b
           WHERE a.status='open' AND b.status='open' GROUP BY l.status"""
    )
    return {r["status"]: r["n"] for r in rows}


async def review_link(pool: asyncpg.Pool, link_id: int, status: str, relation: str | None, note: str | None) -> dict[str, Any] | None:
    r = await pool.fetchrow(
        """UPDATE market_link l SET status=$2, relation=COALESCE($3, relation), note=COALESCE($4, note),
                  reviewed_at=now(), updated_at=now(),
                  rules_hash_a=(SELECT rules_hash FROM market WHERE id=l.market_a),
                  rules_hash_b=(SELECT rules_hash FROM market WHERE id=l.market_b)
           WHERE id=$1 RETURNING id, status, relation""",
        link_id, status, relation, note,
    )
    return dict(r) if r else None


async def links_for_arb(pool: asyncpg.Pool, include_suggested: bool) -> list[dict[str, Any]]:
    statuses = ["confirmed", "suggested"] if include_suggested else ["confirmed"]
    rows = await pool.fetch(
        f"""SELECT {LINK_COLUMNS}, {_SIDE.format(p='a')}, {_SIDE.format(p='b')}
            FROM market_link l JOIN market a ON a.id = l.market_a JOIN market b ON b.id = l.market_b
            WHERE l.status = ANY($1) AND a.status='open' AND b.status='open'""",
        statuses,
    )
    return [_split_link(r) for r in rows]


async def kept_ids(pool: asyncpg.Pool, venue: str) -> frozenset[str]:
    """Venue market IDs that must stay tracked: watched markets and non-rejected links."""
    rows = await pool.fetch(
        """SELECT m.venue_market_id FROM market m WHERE m.venue = $1 AND (
               m.id IN (SELECT market_id FROM watchlist_item)
            OR m.id IN (SELECT market_a FROM market_link WHERE status <> 'rejected')
            OR m.id IN (SELECT market_b FROM market_link WHERE status <> 'rejected'))""",
        venue,
    )
    return frozenset(r["venue_market_id"] for r in rows)


async def record_arb(pool: asyncpg.Pool, opps: list[Any], tracked_links: set[int]) -> None:
    """Open/extend episodes for positive edges; close episodes whose edge is gone (R5.5)."""
    positive = {(o.link_id, o.direction): o for o in opps if o.edge > 0}
    async with pool.acquire() as conn:
        async with conn.transaction():
            for (link_id, direction), o in positive.items():
                await conn.execute(
                    """INSERT INTO arb_opportunity (link_id, direction, best_edge, last_edge, last_cost)
                       VALUES ($1, $2, $3, $3, $4)
                       ON CONFLICT (link_id, direction) WHERE closed_at IS NULL DO UPDATE SET
                           last_seen = now(), last_edge = EXCLUDED.last_edge, last_cost = EXCLUDED.last_cost,
                           best_edge = GREATEST(arb_opportunity.best_edge, EXCLUDED.best_edge)""",
                    link_id, direction, o.edge, o.cost,
                )
            open_rows = await conn.fetch("SELECT id, link_id, direction FROM arb_opportunity WHERE closed_at IS NULL")
            stale = [r["id"] for r in open_rows if (r["link_id"], r["direction"]) not in positive and r["link_id"] in tracked_links]
            if stale:
                await conn.execute("UPDATE arb_opportunity SET closed_at = now() WHERE id = ANY($1)", stale)


async def arb_episodes(pool: asyncpg.Pool) -> dict[tuple[int, str], dict[str, Any]]:
    rows = await pool.fetch("SELECT * FROM arb_opportunity WHERE closed_at IS NULL")
    return {(r["link_id"], r["direction"]): dict(r) for r in rows}


# ---------------------------------------------------------------- news

async def insert_news(pool: asyncpg.Pool, items: list[Any]) -> list[tuple[int, Any]]:
    """Insert news items; returns (id, item) for the ones that were new."""
    new = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            for it in items:
                nid = await conn.fetchval(
                    """INSERT INTO news_item (url, title_key, source, title, summary, published_at)
                       VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING RETURNING id""",
                    it.url, it.title_key, it.source, it.title, it.summary, it.published_at,
                )
                if nid is not None:
                    new.append((nid, it))
    return new


async def insert_news_links(pool: asyncpg.Pool, rows: list[tuple[int, str, float, list[str]]]) -> None:
    if rows:
        await pool.executemany(
            """INSERT INTO news_market (news_id, market_id, score, matched_terms) VALUES ($1,$2,$3,$4)
               ON CONFLICT DO NOTHING""",
            rows,
        )


async def markets_for_news(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch("SELECT id, title, outcome_label, event_title FROM market WHERE status='open'")
    return [dict(r) for r in rows]


async def watched_ids(pool: asyncpg.Pool, user_id: str = "me") -> set[str]:
    return {r["market_id"] for r in await pool.fetch("SELECT market_id FROM watchlist_item WHERE user_id=$1", user_id)}


async def notify(pool: asyncpg.Pool, market_id: str | None, kind: str, message: str, user_id: str = "me") -> dict[str, Any]:
    r = await pool.fetchrow(
        "INSERT INTO notification (user_id, market_id, kind, message) VALUES ($1,$2,$3,$4) RETURNING *",
        user_id, market_id, kind, message,
    )
    return dict(r)


async def prune_news(pool: asyncpg.Pool, days: int) -> int:
    res = await pool.execute("DELETE FROM news_item WHERE published_at < now() - make_interval(days => $1)", days)
    return int(res.split()[-1])


async def list_news(
    pool: asyncpg.Pool, scope: str = "watchlist", market_id: str | None = None, limit: int = 50, user_id: str = "me"
) -> list[dict[str, Any]]:
    """Recent news with the markets each item matched. scope: 'watchlist' | 'matched' | 'all'."""
    args: list[Any] = [limit]
    if market_id:
        args.append(market_id)
        cond = f"n.id IN (SELECT news_id FROM news_market WHERE market_id = ${len(args)})"
    elif scope == "watchlist":
        args.append(user_id)
        cond = f"n.id IN (SELECT nm.news_id FROM news_market nm JOIN watchlist_item w ON w.market_id = nm.market_id AND w.user_id = ${len(args)})"
    elif scope == "matched":
        cond = "n.id IN (SELECT news_id FROM news_market)"
    else:
        cond = "TRUE"
    rows = await pool.fetch(
        f"""SELECT n.id, n.url, n.source, n.title, n.summary, n.published_at,
                   COALESCE(json_agg(json_build_object(
                       'id', m.id, 'title', m.title, 'outcome_label', m.outcome_label, 'venue', m.venue,
                       'score', nm.score, 'terms', nm.matched_terms) ORDER BY nm.score DESC)
                     FILTER (WHERE m.id IS NOT NULL), '[]') AS markets
            FROM news_item n
            LEFT JOIN news_market nm ON nm.news_id = n.id
            LEFT JOIN market m ON m.id = nm.market_id AND m.status = 'open'
            WHERE {cond}
            GROUP BY n.id ORDER BY n.published_at DESC LIMIT $1""",
        *args,
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- AI

async def ai_candidates(pool: asyncpg.Pool, limit: int = 60) -> list[dict[str, Any]]:
    """Liquid, contested, soon-enough markets with fresh quotes, plus their recent news count."""
    rows = await pool.fetch(
        f"""SELECT {MARKET_COLUMNS},
                   (SELECT count(*) FROM news_market nm JOIN news_item n ON n.id = nm.news_id
                     WHERE nm.market_id = market.id AND n.published_at > now() - interval '48 hours') AS news_count
            FROM market
            WHERE status = 'open' AND mid BETWEEN 0.05 AND 0.95 AND yes_ask IS NOT NULL AND yes_bid IS NOT NULL
              AND (spread IS NULL OR spread <= 0.06)
              AND quote_ts > now() - interval '1 hour'
              AND (close_ts IS NULL OR close_ts BETWEEN now() + interval '12 hours' AND now() + interval '180 days')
            ORDER BY volume_24h_usd DESC NULLS LAST LIMIT $1""",
        limit,
    )
    return [dict(r) for r in rows]


async def market_full(pool: asyncpg.Pool, market_id: str) -> dict[str, Any] | None:
    return await get_market_row(pool, market_id)


async def daily_closes(pool: asyncpg.Pool, market_id: str, days: int = 14) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """SELECT date_trunc('day', ts) AS t, (array_agg(price ORDER BY ts DESC))[1] AS close
           FROM price_tick WHERE market_id = $1 AND ts > now() - make_interval(days => $2)
           GROUP BY 1 ORDER BY 1""",
        market_id, days,
    )
    return [dict(r) for r in rows]


async def headlines(pool: asyncpg.Pool, market_id: str, days: int = 7, limit: int = 10) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """SELECT n.id, n.source, n.title, n.url, n.published_at FROM news_item n
           JOIN news_market nm ON nm.news_id = n.id
           WHERE nm.market_id = $1 AND n.published_at > now() - make_interval(days => $2)
           ORDER BY n.published_at DESC LIMIT $3""",
        market_id, days, limit,
    )
    return [dict(r) for r in rows]


async def create_batch(pool: asyncpg.Pool, kind: str, market_ids: list[str] | None = None) -> dict[str, Any]:
    r = await pool.fetchrow("INSERT INTO ai_batch (kind, market_ids) VALUES ($1, $2) RETURNING *", kind, market_ids or [])
    return dict(r)


async def update_batch(pool: asyncpg.Pool, batch_id: int, **fields: Any) -> None:
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    await pool.execute(f"UPDATE ai_batch SET {sets} WHERE id = $1", batch_id, *fields.values())


async def get_batch(pool: asyncpg.Pool, batch_id: int) -> dict[str, Any] | None:
    r = await pool.fetchrow("SELECT * FROM ai_batch WHERE id = $1", batch_id)
    return dict(r) if r else None


async def latest_batch(pool: asyncpg.Pool, kind: str = "picks") -> dict[str, Any] | None:
    r = await pool.fetchrow("SELECT * FROM ai_batch WHERE kind = $1 ORDER BY id DESC LIMIT 1", kind)
    return dict(r) if r else None


async def insert_analysis(pool: asyncpg.Pool, a: dict[str, Any]) -> int:
    cols = list(a)
    return await pool.fetchval(
        f"INSERT INTO analysis ({', '.join(cols)}) VALUES ({', '.join(f'${i + 1}' for i in range(len(cols)))}) RETURNING id",
        *a.values(),
    )


_ANALYSIS_VIEW = """
    a.*, m.title, m.outcome_label, m.event_title, m.venue, m.category, m.url, m.close_ts,
    m.yes_bid, m.yes_ask, m.mid, m.quote_ts
"""


async def batch_analyses(pool: asyncpg.Pool, batch_id: int) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        f"""SELECT {_ANALYSIS_VIEW} FROM analysis a JOIN market m ON m.id = a.market_id
            WHERE a.batch_id = $1 ORDER BY a.status, a.opportunity_score DESC NULLS LAST""",
        batch_id,
    )
    return [dict(r) for r in rows]


async def latest_analysis(pool: asyncpg.Pool, market_id: str) -> dict[str, Any] | None:
    r = await pool.fetchrow(
        f"""SELECT {_ANALYSIS_VIEW} FROM analysis a JOIN market m ON m.id = a.market_id
            WHERE a.market_id = $1 ORDER BY a.created_at DESC LIMIT 1""",
        market_id,
    )
    return dict(r) if r else None


async def llm_usage_today(pool: asyncpg.Pool) -> dict[str, Any]:
    r = await pool.fetchrow(
        """SELECT count(*) AS calls, count(*) FILTER (WHERE ok) AS ok,
                  coalesce(sum(prompt_tokens), 0) AS prompt_tokens, coalesce(sum(completion_tokens), 0) AS completion_tokens
           FROM llm_call WHERE ts >= date_trunc('day', now())"""
    )
    return dict(r)


async def links_needing_llm(pool: asyncpg.Pool, limit: int) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        f"""SELECT {LINK_COLUMNS}, {_SIDE.format(p='a')}, {_SIDE.format(p='b')}
            FROM market_link l JOIN market a ON a.id = l.market_a JOIN market b ON b.id = l.market_b
            WHERE l.status = 'suggested' AND l.llm_relation IS NULL AND a.status = 'open' AND b.status = 'open'
            ORDER BY l.score DESC LIMIT $1""",
        limit,
    )
    return [_split_link(r) for r in rows]


async def set_link_llm(pool: asyncpg.Pool, link_id: int, relation: str, confidence: float, differences: list[str], model: str) -> None:
    await pool.execute(
        """UPDATE market_link SET llm_relation=$2, llm_confidence=$3, llm_differences=$4, llm_model=$5, updated_at=now()
           WHERE id=$1""",
        link_id, relation, confidence, differences, model,
    )
