"""Offline demo venue: deterministic fake markets with random-walk prices.

Lets you run the full stack with no network access (VENUES=demo). Every market is
clearly labeled DEMO in the UI; it never mixes with real venues' data.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from predictarb.models import Market, PricePoint, Quote
from predictarb.venues.base import VenueAdapter

_TEMPLATES = [
    ("Economics", "Will CPI YoY for {m} come in above {x}%?", ["2.6", "2.8", "3.0"]),
    ("Economics", "Will the Fed cut rates at the {m} meeting?", [None]),
    ("Politics", "Will the {x} win the 2028 presidential election?", ["Democratic candidate", "Republican candidate"]),
    ("Politics", "Will the Senate pass the {x} bill by {m}?", ["infrastructure", "crypto market structure", "AI safety"]),
    ("Crypto", "Will Bitcoin be above ${x} on {m} 31?", ["100,000", "120,000", "150,000"]),
    ("Crypto", "Will Ethereum close above ${x} on {m} 31?", ["4,000", "5,000"]),
    ("Sports", "Will the {x} win the championship?", ["Aces", "Golden Knights", "Raiders"]),
    ("Weather", "Will Las Vegas hit {x}°F on any day in {m}?", ["70", "80"]),
    ("Tech", "Will {x} release a new frontier model before {m} 31?", ["OpenAI", "Google", "Anthropic"]),
    ("Culture", "Will {x} top the box office on opening weekend in {m}?", ["the next Marvel film", "an original animated film"]),
]
_MONTHS = ["December", "January", "February", "March"]


def _catalog() -> list[dict[str, Any]]:
    rng = random.Random(42)
    rows = []
    n = 0
    for cat, tpl, xs in _TEMPLATES:
        for x in xs:
            for m in _MONTHS[: rng.randint(1, 3)]:
                n += 1
                rows.append(
                    {
                        "id": f"DEMO-{n:03d}",
                        "category": cat,
                        "title": tpl.format(x=x, m=m),
                        "p0": rng.uniform(0.08, 0.92),
                        "vol": rng.uniform(0.004, 0.03),
                        "volume": rng.uniform(2_000, 400_000),
                        "days": rng.randint(5, 120),
                    }
                )
    return rows


class DemoAdapter(VenueAdapter):
    venue_id = "demo"
    venue_name = "Demo data (offline)"

    def __init__(self, *_: Any, **__: Any):
        self.rows = _catalog()
        self.state: dict[str, float] = {r["id"]: r["p0"] for r in self.rows}
        self.rng = random.Random()

    def _quote(self, r: dict[str, Any], now: datetime) -> Quote:
        p = self.state[r["id"]]
        half = max(0.005, 0.02 * (1 - abs(p - 0.5)))
        return Quote(
            venue="demo",
            venue_market_id=r["id"],
            yes_bid=round(max(0.01, p - half), 2),
            yes_ask=round(min(0.99, p + half), 2),
            yes_bid_size=round(r["volume"] / 400, 0),
            yes_ask_size=round(r["volume"] / 500, 0),
            last_price=round(p, 2),
            change_24h=round(p - r["p0"], 4),
            volume_24h=round(r["volume"], 2),
            volume_24h_usd=round(r["volume"], 2),
            liquidity_usd=round(r["volume"] / 4, 2),
            ts=now,
        )

    async def fetch_markets(self) -> list[Market]:
        now = datetime.now(timezone.utc)
        return [
            Market(
                venue="demo",
                venue_market_id=r["id"],
                title=r["title"],
                category=r["category"],
                tags=["demo"],
                rules_text="DEMO market generated offline. Resolves YES if the described event occurs. Not a real contract.",
                resolution_source="None (synthetic)",
                close_ts=now + timedelta(days=r["days"]),
                volume_unit="usd",
                total_volume=round(r["volume"] * 12, 2),
                url=None,
                quote=self._quote(r, now),
            )
            for r in self.rows
        ]

    async def fetch_quotes(self) -> list[Quote]:
        now = datetime.now(timezone.utc)
        out = []
        for r in self.rows:
            p = self.state[r["id"]]
            logit = math.log(p / (1 - p)) + self.rng.gauss(0, r["vol"] * 4)
            if self.rng.random() < 0.01:  # occasional news jump
                logit += self.rng.choice([-1, 1]) * self.rng.uniform(0.3, 0.9)
            self.state[r["id"]] = min(0.98, max(0.02, 1 / (1 + math.exp(-logit))))
            out.append(self._quote(r, now))
        return out

    async def fetch_history(self, market: dict[str, Any], days: int) -> list[PricePoint]:
        rng = random.Random(market["venue_market_id"])
        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        p = self.state.get(market["venue_market_id"], 0.5)
        points = []
        logit = math.log(p / (1 - p))
        for h in range(days * 24):
            points.append(PricePoint(ts=end - timedelta(hours=h), price=round(1 / (1 + math.exp(-logit)), 4)))
            logit += rng.gauss(0, 0.05)
        return list(reversed(points))
