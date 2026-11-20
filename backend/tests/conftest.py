import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://predictarb:predictarb@localhost:5432/predictarb_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def fixture():
    return load


async def _services_up() -> bool:
    import asyncpg
    import redis.asyncio as aioredis

    try:
        conn = await asyncpg.connect(os.environ["DATABASE_URL"], timeout=2)
        await conn.close()
        r = aioredis.from_url(os.environ["REDIS_URL"])
        await r.ping()
        await r.aclose()
        return True
    except Exception:
        return False


@pytest.fixture
async def clean_db():
    """Fresh schema + empty redis db for integration tests; skipped if services are down."""
    if not await _services_up():
        pytest.skip("Postgres/Redis not available")
    import asyncpg
    import redis.asyncio as aioredis

    from predictarb.migrate import migrate

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    await migrate(os.environ["DATABASE_URL"])
    r = aioredis.from_url(os.environ["REDIS_URL"])
    await r.flushdb()
    await r.aclose()
    yield os.environ["DATABASE_URL"]
