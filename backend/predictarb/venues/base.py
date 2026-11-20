"""Venue adapter interface (design doc R3.1) and shared HTTP plumbing.

Sprint 1 adapters are read-only: they list markets, poll quotes and fetch price history.
Order placement methods are deliberately absent until the execution sprint.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

import httpx

from predictarb.models import Market, PricePoint, Quote

log = logging.getLogger(__name__)


class TokenBucket:
    """Client-side rate limiter (R3.4)."""

    def __init__(self, rate_per_s: float, burst: int | None = None):
        self.rate = rate_per_s
        self.capacity = burst or max(1, int(rate_per_s))
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class VenueError(RuntimeError):
    pass


class HttpVenue:
    """httpx client with rate limiting and retry/backoff on 429 and 5xx (R3.3, R3.4)."""

    def __init__(self, base_url: str, rate_per_s: float, client: httpx.AsyncClient | None = None, retries: int = 4):
        self.base_url = base_url.rstrip("/")
        self.bucket = TokenBucket(rate_per_s)
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0), headers={"User-Agent": "predictarb/0.1"})
        self.retries = retries

    async def get_json(self, path: str, params: dict[str, Any] | None = None, base: str | None = None) -> Any:
        url = (base.rstrip("/") if base else self.base_url) + path
        delay = 1.0
        for attempt in range(self.retries + 1):
            await self.bucket.acquire()
            try:
                resp = await self.client.get(url, params={k: v for k, v in (params or {}).items() if v is not None})
            except httpx.TransportError as e:
                if attempt == self.retries:
                    raise VenueError(f"GET {url}: {e}") from e
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise VenueError(f"GET {url} -> {resp.status_code}: {resp.text[:200]}")
                retry_after = resp.headers.get("retry-after")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            await asyncio.sleep(delay + random.random() * 0.5)
            delay = min(delay * 2, 30)
        raise VenueError(f"GET {url}: retries exhausted")

    async def aclose(self) -> None:
        await self.client.aclose()


class VenueAdapter(ABC):
    venue_id: str
    venue_name: str

    @abstractmethod
    async def fetch_markets(self) -> list[Market]:
        """Full catalog of open markets, each with its current quote when the venue returns one."""

    @abstractmethod
    async def fetch_quotes(self) -> list[Quote]:
        """Current top-of-book for open markets. May be the same call as fetch_markets."""

    @abstractmethod
    async def fetch_history(self, market: dict[str, Any], days: int) -> list[PricePoint]:
        """Historical prices for one market (catalog row as dict)."""

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        pass


# ---------------------------------------------------------------- parsing helpers

def to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_prob(v: Any) -> float | None:
    """Venue price in dollars/probability -> probability in [0, 1], or None for empty books."""
    f = to_float(v)
    if f is None:
        return None
    if f > 1.0:  # defensively handle cent-denominated values
        f = f / 100.0
    return round(min(max(f, 0.0), 1.0), 4)


def parse_ts(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
