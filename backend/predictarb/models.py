"""Venue-neutral domain models. Prices are probabilities in [0, 1]."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


def market_id(venue: str, venue_market_id: str) -> str:
    return f"{venue}:{venue_market_id}"


def rules_hash(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]


class Quote(BaseModel):
    """Top-of-book snapshot for the YES side of one market."""

    venue: str
    venue_market_id: str
    yes_bid: float | None = None
    yes_ask: float | None = None
    yes_bid_size: float | None = None  # contracts at the best bid/ask, when the venue reports it
    yes_ask_size: float | None = None
    last_price: float | None = None
    change_24h: float | None = None  # absolute change in probability
    volume_24h: float | None = None  # in the venue's native unit (see Market.volume_unit)
    volume_24h_usd: float | None = None
    open_interest: float | None = None
    liquidity_usd: float | None = None
    ts: datetime | None = None

    @property
    def id(self) -> str:
        return market_id(self.venue, self.venue_market_id)

    @property
    def mid(self) -> float | None:
        if self.yes_bid is not None and self.yes_ask is not None and self.yes_ask > 0:
            return round((self.yes_bid + self.yes_ask) / 2, 4)
        return self.last_price

    @property
    def spread(self) -> float | None:
        if self.yes_bid is not None and self.yes_ask is not None and self.yes_ask > 0:
            return round(self.yes_ask - self.yes_bid, 4)
        return None

    def same_prices(self, other: "Quote | None") -> bool:
        if other is None:
            return False
        return (self.yes_bid, self.yes_ask, self.last_price) == (
            other.yes_bid,
            other.yes_ask,
            other.last_price,
        )


class Market(BaseModel):
    """Unified catalog record (design doc R4.1)."""

    venue: str
    venue_market_id: str
    event_id: str | None = None
    event_title: str | None = None
    title: str
    outcome_label: str | None = None
    url: str | None = None
    category: str = "Other"
    tags: list[str] = Field(default_factory=list)
    rules_text: str | None = None
    resolution_source: str | None = None
    close_ts: datetime | None = None
    status: str = "open"
    volume_unit: str = "usd"  # 'usd' or 'contracts'
    total_volume: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    quote: Quote | None = None

    @property
    def id(self) -> str:
        return market_id(self.venue, self.venue_market_id)

    @property
    def rules_hash(self) -> str | None:
        return rules_hash(self.rules_text)


class PricePoint(BaseModel):
    ts: datetime
    price: float
    yes_bid: float | None = None
    yes_ask: float | None = None
    volume: float | None = None
