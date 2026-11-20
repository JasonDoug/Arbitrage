"""Order books: fetch, normalize to YES/NO ladders, and walk them (VWAP fills).

Every book is normalized to the YES token:
  yes_bids  best (highest) first   yes_asks  best (lowest) first
The NO side is derived (binary market): NO ask = 1 - YES bid, NO bid = 1 - YES ask.

Kalshi's book endpoint returns bids only (YES bids and NO bids); a NO bid at p is a YES ask
at 1 - p. Its auth requirement is documented inconsistently, so on 401 we fall back to the
top-of-book price and size Kalshi reports on each market ("top_of_book" source).
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from predictarb import arb
from predictarb.config import Settings
from predictarb.venues.base import to_float

log = logging.getLogger(__name__)

Level = tuple[float, float]  # (price 0-1, size in contracts)


@dataclass
class Book:
    market_id: str
    yes_bids: list[Level] = field(default_factory=list)
    yes_asks: list[Level] = field(default_factory=list)
    source: str = "book"  # 'book' | 'top_of_book' | 'demo'
    fetched_at: float = field(default_factory=time.time)

    def asks(self, side: str) -> list[Level]:
        """Levels you can BUY `side` at, best first."""
        if side == "YES":
            return self.yes_asks
        return [(round(1 - p, 4), s) for p, s in self.yes_bids]

    def bids(self, side: str) -> list[Level]:
        """Levels you can SELL `side` into, best first."""
        if side == "YES":
            return self.yes_bids
        return [(round(1 - p, 4), s) for p, s in self.yes_asks]

    def as_dict(self, depth: int = 10) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "source": self.source,
            "yes_bids": [list(x) for x in self.yes_bids[:depth]],
            "yes_asks": [list(x) for x in self.yes_asks[:depth]],
        }


def _levels(raw: list[Any]) -> list[Level]:
    out = []
    for lv in raw or []:
        if isinstance(lv, dict):
            p, s = to_float(lv.get("price")), to_float(lv.get("size"))
        else:
            p, s = to_float(lv[0]), to_float(lv[1])
        if p is not None and s and 0 < p < 1 and s > 0:
            out.append((round(p, 4), s))
    return out


def parse_polymarket(market_id: str, data: dict[str, Any]) -> Book:
    return Book(
        market_id,
        yes_bids=sorted(_levels(data.get("bids")), key=lambda x: -x[0]),
        yes_asks=sorted(_levels(data.get("asks")), key=lambda x: x[0]),
    )


def parse_kalshi(market_id: str, data: dict[str, Any]) -> Book:
    ob = data.get("orderbook_fp") or data.get("orderbook") or {}
    yes_bids = _levels(ob.get("yes_dollars") or [])
    no_bids = _levels(ob.get("no_dollars") or [])
    return Book(
        market_id,
        yes_bids=sorted(yes_bids, key=lambda x: -x[0]),
        yes_asks=sorted(((round(1 - p, 4), s) for p, s in no_bids), key=lambda x: x[0]),
    )


def top_of_book(m: dict[str, Any], source: str = "top_of_book") -> Book:
    """One level per side from the market row. Unknown size -> treated as 0 (can't fill)."""
    bids = [(m["yes_bid"], m.get("yes_bid_size") or 0.0)] if m.get("yes_bid") else []
    asks = [(m["yes_ask"], m.get("yes_ask_size") or 0.0)] if m.get("yes_ask") else []
    return Book(m["id"], yes_bids=[b for b in bids if b[1] > 0], yes_asks=[a for a in asks if a[1] > 0], source=source)


class BookFetcher:
    """Fetches live books with a short cache (API process and worker each own one)."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None, ttl_s: float = 3.0):
        self.s = settings
        self.client = client or httpx.AsyncClient(timeout=10, headers={"User-Agent": "predictarb/0.1"})
        self.ttl = ttl_s
        self.cache: dict[str, Book] = {}
        self.kalshi_needs_auth = False

    async def get(self, m: dict[str, Any]) -> Book:
        hit = self.cache.get(m["id"])
        if hit and time.time() - hit.fetched_at < self.ttl:
            return hit
        book = await self._fetch(m)
        self.cache[m["id"]] = book
        return book

    async def _fetch(self, m: dict[str, Any]) -> Book:
        venue = m["venue"]
        try:
            if venue == "polymarket":
                tokens = (m.get("extra") or {}).get("clob_token_ids") or []
                if tokens:
                    r = await self.client.get(f"{self.s.polymarket_clob_url}/book", params={"token_id": tokens[0]})
                    r.raise_for_status()
                    return parse_polymarket(m["id"], r.json())
            elif venue == "kalshi" and not self.kalshi_needs_auth:
                r = await self.client.get(f"{self.s.kalshi_base_url}/markets/{m['venue_market_id']}/orderbook", params={"depth": 20})
                if r.status_code in (401, 403):
                    self.kalshi_needs_auth = True
                    log.warning("kalshi orderbook requires auth; using top-of-book sizes instead")
                else:
                    r.raise_for_status()
                    return parse_kalshi(m["id"], r.json())
        except httpx.HTTPError as e:
            log.warning("book fetch %s failed (%s); using top of book", m["id"], e)
        return top_of_book(m, "demo" if venue == "demo" else "top_of_book")

    async def resolution(self, m: dict[str, Any]) -> str | None:
        """'yes' / 'no' once the venue reports a final result, else None."""
        try:
            if m["venue"] == "kalshi":
                r = await self.client.get(f"{self.s.kalshi_base_url}/markets/{m['venue_market_id']}")
                r.raise_for_status()
                mk = (r.json() or {}).get("market") or {}
                if mk.get("result") in ("yes", "no") and mk.get("status") in ("determined", "finalized", "amended", "settled"):
                    return mk["result"]
            elif m["venue"] == "polymarket":
                r = await self.client.get(f"{self.s.polymarket_gamma_url}/markets/{m['venue_market_id']}")
                r.raise_for_status()
                mk = r.json() or {}
                if mk.get("closed"):
                    prices = mk.get("outcomePrices")
                    prices = json.loads(prices) if isinstance(prices, str) else (prices or [])
                    vals = [to_float(x) for x in prices]
                    if vals[:2] == [1.0, 0.0]:
                        return "yes"
                    if vals[:2] == [0.0, 1.0]:
                        return "no"
        except (httpx.HTTPError, ValueError) as e:
            log.warning("resolution check %s failed: %s", m["id"], e)
        return None

    async def aclose(self) -> None:
        await self.client.aclose()


# ---------------------------------------------------------------- walking the book


@dataclass
class Fill:
    qty: float
    avg_price: float
    levels: list[Level]  # (price, qty) actually taken

    @property
    def cost(self) -> float:
        return sum(p * q for p, q in self.levels)


def walk(levels: list[Level], qty: float, limit: float | None = None, buying: bool = True) -> Fill:
    """Take liquidity best-first up to qty, never past `limit` (max for buys, min for sells)."""
    taken: list[Level] = []
    left = qty
    for p, s in levels:
        if left <= 1e-9:
            break
        if limit is not None and ((buying and p > limit + 1e-9) or (not buying and p < limit - 1e-9)):
            break
        q = min(s, left)
        taken.append((p, q))
        left -= q
    filled = qty - left
    avg = sum(p * q for p, q in taken) / filled if filled > 0 else 0.0
    return Fill(round(filled, 4), round(avg, 6), taken)


def taker_fee(venue: str, levels: list[Level], category: str, pm_rates: dict[str, float] | None = None) -> float:
    """Fee for one order filled across `levels`. Kalshi rounds up to the cent per order."""
    if venue == "kalshi":
        raw = sum(arb.KALSHI_TAKER_RATE * q * p * (1 - p) for p, q in levels)
        return math.ceil(round(raw * 100, 6)) / 100 if raw > 0 else 0.0
    if venue == "polymarket":
        return round(sum(arb.polymarket_fee_per_contract(p, category, pm_rates) * q for p, q in levels), 4)
    return 0.0


def best_pair_size(
    asks_a: list[Level], asks_b: list[Level], venue_a: str, venue_b: str, cat_a: str, cat_b: str,
    pm_rates: dict[str, float] | None = None, max_qty: float = 100_000,
) -> dict[str, Any]:
    """For a two-leg arb (buy one ask ladder on each venue), find the size with the most total
    dollar edge. Walks both ladders level by level; per-contract fees use the exact formula."""

    def fee(venue: str, p: float, cat: str) -> float:
        if venue == "kalshi":
            return arb.kalshi_fee_per_contract(p)
        if venue == "polymarket":
            return arb.polymarket_fee_per_contract(p, cat, pm_rates)
        return 0.0

    a = [list(x) for x in asks_a]
    b = [list(x) for x in asks_b]
    i = j = 0
    qty = total = 0.0
    best = {"qty": 0.0, "edge_dollars": 0.0, "avg_cost": None}
    cost_sum = 0.0
    while i < len(a) and j < len(b) and qty < max_qty:
        pa, sa = a[i]
        pb, sb = b[j]
        unit = 1 - pa - pb - fee(venue_a, pa, cat_a) - fee(venue_b, pb, cat_b)
        if unit <= 0:
            break
        q = min(sa, sb, max_qty - qty)
        qty += q
        total += unit * q
        cost_sum += (pa + pb) * q
        best = {"qty": round(qty, 2), "edge_dollars": round(total, 2), "avg_cost": round(cost_sum / qty, 4)}
        a[i][1] -= q
        b[j][1] -= q
        if a[i][1] <= 1e-9:
            i += 1
        if b[j][1] <= 1e-9:
            j += 1
    return best
