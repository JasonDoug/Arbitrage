"""Cross-venue arbitrage math (design doc section 5) on top-of-book quotes.

For a linked pair, buying complementary sides on the two venues locks in $1 at resolution.
The edge is what's left of that $1 after paying both asks and both venues' taker fees.
Prices are probabilities (0-1); fees are per contract, in dollars.

Sprint 2a uses top-of-book only (no depth), so sizes aren't shown yet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# Kalshi taker fee: round_up(0.07 x C x P x (1-P)) per order (published fee schedule).
KALSHI_TAKER_RATE = 0.07

# Polymarket taker fee: C x feeRate x p x (1-p), rate by category. Configurable; these
# defaults were the published rates as of Nov 2026 and may change.
POLYMARKET_RATES = {
    "Crypto": 0.072,
    "Economics": 0.05,
    "Culture": 0.05,
    "Weather": 0.05,
    "Other": 0.05,
    "Politics": 0.04,
    "Tech": 0.04,
    "Sports": 0.03,
}


def kalshi_fee(price: float, contracts: float = 1.0, rate: float = KALSHI_TAKER_RATE) -> float:
    """Fee in dollars for one order. Rounded up to the cent, as Kalshi does per order."""
    raw = rate * contracts * price * (1 - price)
    return math.ceil(round(raw * 100, 6)) / 100 if contracts >= 1 else raw


def kalshi_fee_per_contract(price: float, rate: float = KALSHI_TAKER_RATE) -> float:
    """Exact per-contract fee before per-order rounding (what large orders converge to)."""
    return rate * price * (1 - price)


def polymarket_fee_per_contract(price: float, category: str, rates: dict[str, float] | None = None) -> float:
    rate = (rates or POLYMARKET_RATES).get(category, (rates or POLYMARKET_RATES).get("Other", 0.05))
    return rate * price * (1 - price)


@dataclass
class Leg:
    venue: str
    market_id: str
    side: str  # 'YES' or 'NO'
    price: float  # ask paid for this side
    fee: float  # per-contract fee


@dataclass
class Opportunity:
    link_id: int
    direction: str
    legs: list[Leg]
    cost: float  # sum of asks
    fees: float
    edge: float  # 1 - cost - fees, per contract
    annualized: float | None
    days_to_close: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "link_id": self.link_id,
            "direction": self.direction,
            "legs": [leg.__dict__ for leg in self.legs],
            "cost": round(self.cost, 4),
            "fees": round(self.fees, 4),
            "edge": round(self.edge, 4),
            "annualized": None if self.annualized is None else round(self.annualized, 4),
            "days_to_close": None if self.days_to_close is None else round(self.days_to_close, 1),
        }


def _asks(m: dict[str, Any]) -> tuple[float | None, float | None]:
    """(YES ask, NO ask). NO ask = 1 - YES bid on a binary book."""
    yes_ask = m.get("yes_ask")
    no_ask = 1 - m["yes_bid"] if m.get("yes_bid") else None
    return yes_ask, (round(no_ask, 4) if no_ask is not None else None)


def _fee(venue: str, price: float, category: str, pm_rates: dict[str, float] | None) -> float:
    if venue == "kalshi":
        return kalshi_fee_per_contract(price)
    if venue == "polymarket":
        return polymarket_fee_per_contract(price, category, pm_rates)
    return 0.0


def evaluate_link(
    link: dict[str, Any],
    a: dict[str, Any],
    b: dict[str, Any],
    now: datetime | None = None,
    pm_rates: dict[str, float] | None = None,
) -> list[Opportunity]:
    """Both directions for a linked pair. Returns every direction with complete quotes
    (including negative edges), so callers can filter and track episodes."""
    now = now or datetime.now(timezone.utc)
    ya, na = _asks(a)
    yb, nb = _asks(b)
    if link["relation"] == "identical":
        combos = [("yes_a_no_b", ("YES", ya), ("NO", nb)), ("no_a_yes_b", ("NO", na), ("YES", yb))]
    else:  # inverse: YES on A pays when NO on B pays, so buy the same side on both venues
        combos = [("yes_a_yes_b", ("YES", ya), ("YES", yb)), ("no_a_no_b", ("NO", na), ("NO", nb))]
    closes = [t for t in (a.get("close_ts"), b.get("close_ts")) if t]
    days = max(((t - now).total_seconds() / 86400 for t in closes), default=None)
    out = []
    for direction, (side_a, pa), (side_b, pb) in combos:
        if pa is None or pb is None or not (0 < pa < 1 and 0 < pb < 1):
            continue
        fa = _fee(a["venue"], pa, a.get("category", "Other"), pm_rates)
        fb = _fee(b["venue"], pb, b.get("category", "Other"), pm_rates)
        cost = pa + pb
        edge = 1 - cost - fa - fb
        ann = None
        if days is not None and cost > 0:
            ann = edge / (cost + fa + fb) * 365 / max(days, 1.0)
        out.append(
            Opportunity(
                link_id=link["id"],
                direction=direction,
                legs=[Leg(a["venue"], a["id"], side_a, pa, round(fa, 5)), Leg(b["venue"], b["id"], side_b, pb, round(fb, 5))],
                cost=cost,
                fees=fa + fb,
                edge=edge,
                annualized=ann,
                days_to_close=days,
            )
        )
    return out


def fresh(side: dict[str, Any], now: datetime, max_age_s: int) -> bool:
    ts = side.get("quote_ts")
    return ts is not None and (now - ts).total_seconds() <= max_age_s


def scan(
    links: list[dict[str, Any]],
    now: datetime | None = None,
    max_age_s: int = 180,
    pm_rates: dict[str, float] | None = None,
) -> list[tuple[dict[str, Any], Opportunity]]:
    """Evaluate every link whose two quotes are fresh. Links come from db.links_for_arb."""
    now = now or datetime.now(timezone.utc)
    out = []
    for link in links:
        a, b = link["a"], link["b"]
        if not (fresh(a, now, max_age_s) and fresh(b, now, max_age_s)):
            continue
        for opp in evaluate_link(link, a, b, now, pm_rates):
            out.append((link, opp))
    return out
