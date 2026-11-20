"""Kalshi read-only adapter (public market data, no API key needed).

Docs: https://docs.kalshi.com  -  prices are FixedPointDollars strings ("0.5600"),
counts are FixedPointCount strings ("10.00"). See design doc section 3.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from predictarb.categories import categorize
from predictarb.config import Settings
from predictarb.models import Market, PricePoint, Quote
from predictarb.venues.base import HttpVenue, VenueAdapter, VenueError, parse_ts, to_float, to_prob

log = logging.getLogger(__name__)

OPEN_STATUSES = {"active", "open", "initialized"}


def _bid(v: Any) -> float | None:
    p = to_prob(v)
    return p if p is not None and p > 0 else None


def _ask(v: Any) -> float | None:
    p = to_prob(v)
    return p if p is not None and 0 < p < 1 else None


def parse_quote(m: dict[str, Any], now: datetime | None = None) -> Quote:
    last = to_prob(m.get("last_price_dollars"))
    prev = to_prob(m.get("previous_price_dollars"))
    yes_bid = _bid(m.get("yes_bid_dollars"))
    yes_ask = _ask(m.get("yes_ask_dollars"))
    vol24 = to_float(m.get("volume_24h_fp"))
    ref_price = last if last else ((yes_bid or 0) + (yes_ask or 0)) / 2 if yes_bid and yes_ask else None
    return Quote(
        venue="kalshi",
        venue_market_id=m["ticker"],
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        last_price=last if last else None,
        change_24h=round(last - prev, 4) if last and prev else None,
        volume_24h=vol24,
        # Each Kalshi contract pays $1, so contracts x price approximates dollars traded.
        volume_24h_usd=round(vol24 * ref_price, 2) if vol24 is not None and ref_price else None,
        open_interest=to_float(m.get("open_interest_fp")),
        ts=now or datetime.now(timezone.utc),
    )


def parse_market(m: dict[str, Any], event: dict[str, Any], series_category: str | None, now: datetime | None = None) -> Market:
    multi = len(event.get("markets") or []) > 1
    rules = "\n\n".join(x for x in (m.get("rules_primary"), m.get("rules_secondary")) if x) or None
    sources = event.get("settlement_sources") or []
    series = event.get("series_ticker") or ""
    title = event.get("title") or m.get("title") or m["ticker"]
    outcome = m.get("yes_sub_title") or None
    status = "open" if (m.get("status") or "").lower() in OPEN_STATUSES else "closed"
    return Market(
        venue="kalshi",
        venue_market_id=m["ticker"],
        event_id=event.get("event_ticker") or m.get("event_ticker"),
        event_title=event.get("sub_title") or None,
        title=title,
        outcome_label=outcome if (multi or (outcome and outcome.lower() not in title.lower())) else None,
        url=f"https://kalshi.com/markets/{series.lower()}" if series else "https://kalshi.com/markets",
        category=categorize([series_category, event.get("category")], title),
        tags=[t for t in [series_category, event.get("category")] if t],
        rules_text=rules,
        resolution_source=", ".join(s.get("name", "") for s in sources if s.get("name")) or None,
        close_ts=parse_ts(m.get("close_time")),
        status=status,
        volume_unit="contracts",
        total_volume=to_float(m.get("volume_fp")),
        extra={
            "series_ticker": series,
            "event_ticker": event.get("event_ticker"),
            "market_type": m.get("market_type"),
            "mutually_exclusive": event.get("mutually_exclusive"),
        },
        quote=parse_quote(m, now),
    )


class KalshiAdapter(VenueAdapter):
    venue_id = "kalshi"
    venue_name = "Kalshi"

    def __init__(self, settings: Settings, http: HttpVenue | None = None):
        self.settings = settings
        self.http = http or HttpVenue(settings.kalshi_base_url, rate_per_s=8)
        self._series_category: dict[str, str] = {}

    async def _load_series_categories(self) -> None:
        """Series carry the category; events' own category field is deprecated."""
        try:
            data = await self.http.get_json("/series")
        except VenueError as e:
            log.warning("kalshi: series list unavailable (%s); falling back to event categories", e)
            return
        for s in data.get("series") or []:
            if s.get("ticker") and s.get("category"):
                self._series_category[s["ticker"]] = s["category"]

    async def fetch_markets(self) -> list[Market]:
        await self._load_series_categories()
        now = datetime.now(timezone.utc)
        out: list[Market] = []
        cursor: str | None = None
        while True:
            data = await self.http.get_json(
                "/events",
                {"status": "open", "with_nested_markets": "true", "limit": 200, "cursor": cursor or None},
            )
            for ev in data.get("events") or []:
                cat = self._series_category.get(ev.get("series_ticker") or "")
                for m in ev.get("markets") or []:
                    if self.settings.kalshi_exclude_mve and m.get("mve_collection_ticker"):
                        continue
                    try:
                        out.append(parse_market(m, ev, cat, now))
                    except (KeyError, ValueError) as e:  # one bad record shouldn't stop the sync
                        log.warning("kalshi: skipping market %s: %s", m.get("ticker"), e)
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    async def fetch_quotes(self) -> list[Quote]:
        now = datetime.now(timezone.utc)
        out: list[Quote] = []
        cursor: str | None = None
        while True:
            data = await self.http.get_json(
                "/markets",
                {
                    "status": "open",
                    "limit": 1000,
                    "cursor": cursor or None,
                    "mve_filter": "exclude" if self.settings.kalshi_exclude_mve else None,
                },
            )
            out.extend(parse_quote(m, now) for m in data.get("markets") or [] if m.get("ticker"))
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    async def fetch_history(self, market: dict[str, Any], days: int) -> list[PricePoint]:
        series = (market.get("extra") or {}).get("series_ticker")
        if not series:
            return []
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        data = await self.http.get_json(
            f"/series/{series}/markets/{market['venue_market_id']}/candlesticks",
            {"start_ts": int(start.timestamp()), "end_ts": int(end.timestamp()), "period_interval": 60},
        )
        return parse_candles(data.get("candlesticks") or [])

    async def aclose(self) -> None:
        await self.http.aclose()


def parse_candles(candles: list[dict[str, Any]]) -> list[PricePoint]:
    points: list[PricePoint] = []
    for c in candles:
        ts = parse_ts(c.get("end_period_ts"))
        price_obj = c.get("price") or {}
        bid = _bid((c.get("yes_bid") or {}).get("close_dollars"))
        ask = _ask((c.get("yes_ask") or {}).get("close_dollars"))
        price = to_prob(price_obj.get("close_dollars"))
        if price is None and bid is not None and ask is not None:
            price = round((bid + ask) / 2, 4)
        if price is None:
            price = to_prob(price_obj.get("previous_dollars"))
        if ts is None or price is None:
            continue
        points.append(PricePoint(ts=ts, price=price, yes_bid=bid, yes_ask=ask, volume=to_float(c.get("volume_fp"))))
    return points
