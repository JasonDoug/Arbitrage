"""Polymarket (global) read-only adapter: Gamma API for catalog + quotes, CLOB for history.

All endpoints used here are public. Trading on the global exchange is geoblocked for US
users, so this venue is a price signal only (design doc section 3).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from predictarb.categories import categorize
from predictarb.config import Settings
from predictarb.models import Market, PricePoint, Quote
from predictarb.venues.base import HttpVenue, VenueAdapter, VenueError, parse_ts, to_float, to_prob

log = logging.getLogger(__name__)

# Gamma caps page size (100 at time of writing); we page by whatever it returns.
PAGE = 100
# prices-history rejects long startTs/endTs spans; fetch in windows and shrink on rejection.
HISTORY_WINDOW = timedelta(days=7)
MIN_HISTORY_WINDOW = timedelta(hours=12)


def _json_list(v: Any) -> list[Any]:
    """Gamma returns outcomes/outcomePrices/clobTokenIds as JSON-encoded strings."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    try:
        parsed = json.loads(v)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _usable(m: dict[str, Any]) -> bool:
    return bool(m.get("enableOrderBook")) and not m.get("closed") and m.get("active", True) is not False


def parse_quote(m: dict[str, Any], now: datetime | None = None) -> Quote:
    prices = _json_list(m.get("outcomePrices"))
    bid = to_prob(m.get("bestBid"))
    ask = to_prob(m.get("bestAsk"))
    last = to_prob(m.get("lastTradePrice"))
    if last is None and prices:
        last = to_prob(prices[0])
    return Quote(
        venue="polymarket",
        venue_market_id=str(m["id"]),
        yes_bid=bid if bid else None,
        yes_ask=ask if ask and ask < 1 else None,
        last_price=last,
        change_24h=to_float(m.get("oneDayPriceChange")),
        volume_24h=to_float(m.get("volume24hr")),
        volume_24h_usd=to_float(m.get("volume24hr")),
        liquidity_usd=to_float(m.get("liquidityNum")) or to_float(m.get("liquidity")),
        ts=now or datetime.now(timezone.utc),
    )


def parse_market(m: dict[str, Any], now: datetime | None = None) -> Market:
    events = m.get("events") or []
    ev = events[0] if events else {}
    outcomes = _json_list(m.get("outcomes"))
    tokens = _json_list(m.get("clobTokenIds"))
    tag_labels = [t.get("label") for t in (m.get("tags") or []) + (ev.get("tags") or []) if isinstance(t, dict)]
    title = m.get("question") or ev.get("title") or str(m["id"])
    first = str(outcomes[0]) if outcomes else None
    slug = ev.get("slug") or m.get("slug")
    return Market(
        venue="polymarket",
        venue_market_id=str(m["id"]),
        event_id=str(ev.get("id")) if ev.get("id") else None,
        event_title=ev.get("title") if ev.get("title") and ev.get("title") != title else None,
        title=title,
        outcome_label=first if first and first.lower() != "yes" else None,
        url=f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com",
        category=categorize([m.get("category"), *tag_labels], title),
        tags=[t for t in dict.fromkeys(tag_labels) if t][:8],
        rules_text=m.get("description") or None,
        resolution_source=m.get("resolutionSource") or None,
        close_ts=parse_ts(m.get("endDate")),
        status="open" if _usable(m) else "closed",
        volume_unit="usd",
        total_volume=to_float(m.get("volumeNum")) or to_float(m.get("volume")),
        extra={
            "condition_id": m.get("conditionId"),
            "slug": m.get("slug"),
            "outcomes": outcomes,
            "clob_token_ids": tokens,
            "neg_risk": ev.get("negRisk"),
        },
        quote=parse_quote(m, now),
    )


class PolymarketAdapter(VenueAdapter):
    venue_id = "polymarket"
    venue_name = "Polymarket (global, read-only)"

    def __init__(self, settings: Settings, http: HttpVenue | None = None):
        self.settings = settings
        self.http = http or HttpVenue(settings.polymarket_gamma_url, rate_per_s=5)

    async def _pages(self, include_tag: bool) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while offset < self.settings.polymarket_max_markets:
            batch = await self.http.get_json(
                "/markets",
                {
                    "closed": "false",
                    "limit": PAGE,
                    "offset": offset,
                    "order": "volume24hr",
                    "ascending": "false",
                    "include_tag": "true" if include_tag else None,
                },
            )
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(batch)
            offset += len(batch)
        return rows[: self.settings.polymarket_max_markets]

    async def fetch_markets(self) -> list[Market]:
        now = datetime.now(timezone.utc)
        out: list[Market] = []
        for m in await self._pages(include_tag=True):
            if not _usable(m) or "id" not in m:
                continue
            try:
                out.append(parse_market(m, now))
            except (KeyError, ValueError) as e:
                log.warning("polymarket: skipping market %s: %s", m.get("id"), e)
        return out

    async def fetch_quotes(self) -> list[Quote]:
        now = datetime.now(timezone.utc)
        return [parse_quote(m, now) for m in await self._pages(include_tag=False) if _usable(m) and "id" in m]

    async def fetch_history(self, market: dict[str, Any], days: int) -> list[PricePoint]:
        tokens = (market.get("extra") or {}).get("clob_token_ids") or []
        if not tokens:
            return []
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        window = HISTORY_WINDOW
        seen: dict[datetime, PricePoint] = {}
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + window, end)
            try:
                data = await self.http.get_json(
                    "/prices-history",
                    {"market": tokens[0], "startTs": int(cursor.timestamp()), "endTs": int(chunk_end.timestamp()), "fidelity": 60},
                    base=self.settings.polymarket_clob_url,
                )
            except VenueError as e:
                if "too long" in str(e) and window > MIN_HISTORY_WINDOW:
                    window = window / 2
                    continue
                raise
            for h in (data or {}).get("history") or []:
                ts, p = parse_ts(h.get("t")), to_prob(h.get("p"))
                if ts is not None and p is not None:
                    seen[ts] = PricePoint(ts=ts, price=p)
            cursor = chunk_end
        return [seen[t] for t in sorted(seen)]

    async def aclose(self) -> None:
        await self.http.aclose()
