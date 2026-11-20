"""Polymarket US (CFTC-regulated) adapter - placeholder.

Polymarket US is a separate exchange from global Polymarket, with its own markets and API
(public market data at gateway.polymarket.us, trading at api.polymarket.us with Ed25519-signed
requests after KYC). Its REST paths weren't verifiable from official docs when Sprint 1 was
written, so this adapter is disabled until they are checked against docs.polymarket.us.
Enabling it via VENUES=polymarket_us logs a warning and returns no data.
"""

from __future__ import annotations

import logging
from typing import Any

from predictarb.models import Market, PricePoint, Quote
from predictarb.venues.base import VenueAdapter

log = logging.getLogger(__name__)


class PolymarketUSAdapter(VenueAdapter):
    venue_id = "polymarket_us"
    venue_name = "Polymarket US"

    def __init__(self, *_: Any, **__: Any):
        log.warning("polymarket_us adapter is a placeholder (see module docstring); it returns no markets")

    async def fetch_markets(self) -> list[Market]:
        return []

    async def fetch_quotes(self) -> list[Quote]:
        return []

    async def fetch_history(self, market: dict[str, Any], days: int) -> list[PricePoint]:
        return []
