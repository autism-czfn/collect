import json
import logging
import os
import sys

from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

# ── User DB pool (USER_DATABASE_URL) ──────────────────────────────────────────

_pool: Optional[asyncpg.Pool] = None


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


# ── Crawl DB pool (CRAWL_DATABASE_URL) ────────────────────────────────────────
# Optional — if not configured, crawl retrieval is silently disabled (Phase 2).

_crawl_pool: Optional[asyncpg.Pool] = None


async def create_crawl_pool() -> None:
    global _crawl_pool
    dsn = os.getenv("CRAWL_DATABASE_URL")
    if not dsn:
        log.warning(
            "CRAWL_DATABASE_URL not set — crawl DB retrieval disabled (Phase 2 feature)"
        )
        return
    _crawl_pool = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=5,
        command_timeout=30,
    )
    log.info("Crawl DB pool ready (min=1 max=5 timeout=30s)")


async def close_crawl_pool() -> None:
    global _crawl_pool
    if _crawl_pool:
        await _crawl_pool.close()
        _crawl_pool = None


def get_crawl_pool() -> Optional[asyncpg.Pool]:
    """Returns None if CRAWL_DATABASE_URL is not configured. Callers must handle None."""
    return _crawl_pool
