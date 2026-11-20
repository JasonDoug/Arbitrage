"""Apply SQL migrations in order. Idempotent."""

from __future__ import annotations

import asyncio
import logging
from importlib import resources

import asyncpg

from predictarb.config import get_settings

log = logging.getLogger(__name__)


async def migrate(dsn: str) -> list[str]:
    conn = await asyncpg.connect(dsn)
    applied: list[str] = []
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migration")}
        files = sorted(
            f for f in resources.files("predictarb.migrations").iterdir() if f.name.endswith(".sql")
        )
        for f in files:
            if f.name in done:
                continue
            async with conn.transaction():
                await conn.execute(f.read_text())
                await conn.execute("INSERT INTO schema_migration (name) VALUES ($1)", f.name)
            applied.append(f.name)
            log.info("applied migration %s", f.name)
    finally:
        await conn.close()
    return applied


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    applied = asyncio.run(migrate(get_settings().database_url))
    print(f"migrations applied: {applied or 'none (up to date)'}")


if __name__ == "__main__":
    run()
