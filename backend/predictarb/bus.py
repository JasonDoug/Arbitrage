"""Event bus on Redis Streams (design doc section 2)."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import redis.asyncio as aioredis

QUOTES = "quotes"
NOTIFICATIONS = "notifications"
BACKFILL_REQUESTS = "backfill.requests"
MAXLEN = 5000


def _default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    raise TypeError(f"not JSON serializable: {type(o)}")


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=_default, separators=(",", ":"))


def connect(url: str) -> aioredis.Redis:
    return aioredis.from_url(url, decode_responses=True, socket_timeout=30, socket_connect_timeout=5, health_check_interval=15)


async def publish(r: aioredis.Redis, stream: str, payload: Any) -> None:
    await r.xadd(stream, {"data": dumps(payload)}, maxlen=MAXLEN, approximate=True)


async def latest_id(r: aioredis.Redis, stream: str) -> str:
    """ID of the newest message in a stream ('0-0' if empty). Use this, not '$', as a
    starting point: '$' re-anchors on every XREAD call and silently drops messages
    published between calls."""
    last = await r.xrevrange(stream, count=1)
    return last[0][0] if last else "0-0"


async def read(r: aioredis.Redis, streams: dict[str, str], block_ms: int = 5000, count: int = 100) -> list[tuple[str, str, Any]]:
    """Returns (stream, message_id, payload) tuples for messages after the given IDs."""
    out: list[tuple[str, str, Any]] = []
    resp = await r.xread(streams, block=block_ms, count=count)
    for stream, messages in resp or []:
        for msg_id, fields in messages:
            out.append((stream, msg_id, json.loads(fields["data"])))
    return out
