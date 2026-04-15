import json
import logging
import os
import sys

import asyncpg

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Register JSON codecs so JSONB columns round-trip as Python dicts."""
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def create_pool() -> None:
    global _pool
    dsn = os.getenv("USER_DATABASE_URL")
    if not dsn:
        log.error(
            "USER_DATABASE_URL is not set — cannot start without a database. Exiting."
        )
        sys.exit(1)
    _pool = await asyncpg.create_pool(
        dsn,
        min_size=2,
        max_size=10,
        command_timeout=30,
        init=_init_connection,
    )
    log.info("DB pool ready (min=2 max=10 timeout=30s)")


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    assert _pool is not None, "DB pool not initialised"
    return _pool
