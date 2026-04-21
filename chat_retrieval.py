"""
chat_retrieval.py — Two-stage retrieval for the chat pipeline.

Stage 1 — Sequential (called before metadata SSE emit):
    retrieve_logs(child_id, pool) → LogResult
    Queries mzhu_test_logs for last 30 days. Returns trigger counts,
    severity avg, time-of-day distribution, co-occurrences, and top-3 triggers.

Stage 2 — Parallel asyncio.gather (called after metadata SSE emit):
    retrieve_crawl(query, sub_queries) → list[dict]
    retrieve_live(query, audience)     → list[dict]

Both Stage 2 sources degrade gracefully:
    - crawl: disabled if CRAWL_DATABASE_URL not set; falls back to tsvector on
      embedding failure; returns [] on any unhandled exception.
    - live:  5 s timeout; returns [] on timeout / 5xx / any exception.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

import asyncpg
import httpx

import db as _db

log = logging.getLogger(__name__)

_SEARCH_SERVICE_URL = os.getenv("SEARCH_SERVICE_URL", "")
_LIVE_TIMEOUT = 15.0  # seconds — headroom for embedding + DB + parallel live search across up to 23 sites

# ── Allowed surface_keys (loaded from sources.json at startup) ────────────────
# Only crawled_items whose surface_key is listed in sources.json are searched
# for the Clinical Evidence panel. Reddit, HackerNews, YouTube community
# channels, etc. are in surfaces.json (for crawling) but NOT in sources.json,
# so they must never appear in Clinical Evidence results.

_SOURCES_JSON = (
    Path(__file__).resolve().parent.parent / "search" / "config" / "sources.json"
)


def _load_allowed_surface_keys() -> list[str]:
    try:
        data = json.loads(_SOURCES_JSON.read_text(encoding="utf-8"))
        keys = [s["surface_key"] for s in data.get("sources", []) if s.get("surface_key")]
        log.info("chat_retrieval: loaded %d allowed surface_keys from sources.json", len(keys))
        return keys
    except Exception as exc:
        log.error("chat_retrieval: failed to load sources.json — no source filter applied: %s", exc)
        return []


_ALLOWED_SURFACE_KEYS: list[str] = _load_allowed_surface_keys()

# ── Fastembed model (lazy singleton) ─────────────────────────────────────────
# fastembed is synchronous; always call via asyncio.to_thread.

_embed_model = None


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        from fastembed import TextEmbedding
        _embed_model = TextEmbedding(model_name="nomic-ai/nomic-embed-text-v1.5")
    return _embed_model


def _embed_sync(text: str) -> list[float]:
    model = _get_embed_model()
    return next(iter(model.embed([text]))).tolist()


def _vec_to_pg(vec: list[float]) -> str:
    """Convert a Python float list to a pgvector text literal '[x,y,…]'."""
    return "[" + ",".join(f"{v:.8f}" for v in vec) + "]"


# ── Log retrieval (Stage 1) ───────────────────────────────────────────────────

def _time_of_day(hour: int) -> str:
    if 6 <= hour <= 11:
        return "morning"
    if 12 <= hour <= 16:
        return "afternoon"
    if 17 <= hour <= 20:
        return "evening"
    return "night"


async def retrieve_logs(child_id: str, pool: asyncpg.Pool) -> dict[str, Any]:
    """Query mzhu_test_logs for the last 30 days.

    Returns:
        {
            "logs":            list of raw log dicts (for evidence SSE),
            "trigger_counts":  {trigger: count, ...} sorted by frequency,
            "top_triggers":    list[str] — top-3 trigger labels (for metadata SSE),
            "avg_severity":    float | None,
            "peak_time_of_day": str,
            "co_occurrences":  list of (trigger_a, trigger_b) pairs,
            "total_events":    int,
        }
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, triggers, severity, tags, context, notes, logged_at
            FROM   mzhu_test_logs
            WHERE  child_id = $1
              AND  logged_at > now() - INTERVAL '30 days'
              AND  NOT voided
            ORDER BY logged_at DESC
            """,
            child_id,
        )

    if not rows:
        return {
            "logs": [],
            "trigger_counts": {},
            "top_triggers": [],
            "avg_severity": None,
            "peak_time_of_day": "unknown",
            "co_occurrences": [],
            "total_events": 0,
        }

    trigger_counts: dict[str, int] = defaultdict(int)
    tod_counts: dict[str, int] = defaultdict(int)
    severities: list[float] = []
    co_occur: dict[tuple, int] = defaultdict(int)
    logs_out: list[dict] = []

    for row in rows:
        triggers: list[str] = row["triggers"] or []
        severity = row["severity"]
        logged_at = row["logged_at"]

        if severity is not None:
            severities.append(float(severity))

        tod = _time_of_day(logged_at.hour)
        tod_counts[tod] += 1

        for t in triggers:
            trigger_counts[t] += 1

        # Co-occurrence: pairs within the same event
        sorted_t = sorted(triggers)
        for i in range(len(sorted_t)):
            for j in range(i + 1, len(sorted_t)):
                co_occur[(sorted_t[i], sorted_t[j])] += 1

        logs_out.append({
            "id": str(row["id"]),   # UUID → string for JSON serialization
            "triggers": triggers,
            "severity": severity,
            "tags": row["tags"] or [],
            "context": row["context"],
            "notes": row["notes"],
            "logged_at": logged_at.isoformat(),
            "time_of_day": tod,
        })

    sorted_triggers = sorted(trigger_counts.items(), key=lambda x: -x[1])
    top_triggers = [t for t, _ in sorted_triggers[:3]]
    peak_tod = max(tod_counts, key=tod_counts.get) if tod_counts else "unknown"
    avg_severity = round(sum(severities) / len(severities), 2) if severities else None
    top_co = sorted(co_occur.items(), key=lambda x: -x[1])[:5]

    return {
        "logs": logs_out,
        "trigger_counts": dict(sorted_triggers),
        "top_triggers": top_triggers,
        "avg_severity": avg_severity,
        "peak_time_of_day": peak_tod,
        "co_occurrences": [list(pair) for pair, _ in top_co],
        "total_events": len(rows),
    }


def build_log_summary(log_result: dict[str, Any]) -> dict[str, Any]:
    """Extract the summary dict consumed by chat_planner."""
    return {
        "total_events":    log_result["total_events"],
        "trigger_counts":  log_result["trigger_counts"],
        "avg_severity":    log_result["avg_severity"],
        "peak_time_of_day": log_result["peak_time_of_day"],
    }


# ── Crawl DB retrieval (Stage 2A) ─────────────────────────────────────────────

async def _crawl_query_vector(
    conn: asyncpg.Connection,
    vec_str: str,
    limit: int,
) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT ci.id,
               ci.url,
               ci.title,
               COALESCE(ci.description, LEFT(ci.content_body, 300)) AS snippet,
               COALESCE(s.organization_name, ci.source)             AS source_name
        FROM   crawled_items ci
        LEFT JOIN surfaces s ON ci.surface_key = s.key
        WHERE  ci.embedding IS NOT NULL
          AND  ci.source    != 'reddit'
          AND  ci.surface_key = ANY($2::text[])
        ORDER BY ci.embedding <=> $1::vector
        LIMIT  $3
        """,
        vec_str,
        _ALLOWED_SURFACE_KEYS,
        limit,
    )
    return [dict(r) for r in rows]


_TSVECTOR_STOPWORDS = frozenset({
    "a","an","the","and","or","but","in","on","at","to","for","of","with",
    "how","what","why","when","where","who","is","are","was","were","be",
    "been","being","have","has","had","do","does","did","will","would",
    "could","should","may","might","shall","can","i","my","your","their",
    "this","that","these","those","it","its","not","no","about","from",
    "safely","respond","deal","handle","manage","help","understand",
    "child","children","autism","autistic",
})


def _keyword_extract(query: str, max_words: int = 4) -> str:
    """Extract up to max_words meaningful nouns from a query for tsvector."""
    tokens = [w.strip(".,;:?!'\"()").lower() for w in query.split()]
    keywords = [w for w in tokens if w and w not in _TSVECTOR_STOPWORDS and len(w) > 2]
    return " ".join(keywords[:max_words]) or query


async def _crawl_query_keyword(
    conn: asyncpg.Connection,
    query: str,
    limit: int,
) -> list[dict]:
    """Keyword search with two-pass tsvector: full query (AND), then key nouns (AND).

    Full plainto_tsquery often returns 0 on long rewritten queries because
    most crawled_items have no content_body, so AND of many terms fails.
    The key-noun pass uses 3-4 core words which have much higher recall.
    """
    # Include description in tsvector — 7 000+ items have description but no content_body.
    # Without description, most rows never match any search.
    _SQL = """
        SELECT ci.id,
               ci.url,
               ci.title,
               COALESCE(ci.description, LEFT(ci.content_body, 300)) AS snippet,
               COALESCE(s.organization_name, ci.source)             AS source_name
        FROM   crawled_items ci
        LEFT JOIN surfaces s ON ci.surface_key = s.key
        WHERE  ci.source    != 'reddit'
          AND  ci.surface_key = ANY($2::text[])
          AND  to_tsvector('english',
                   COALESCE(ci.title, '')       || ' ' ||
                   COALESCE(ci.description, '') || ' ' ||
                   COALESCE(ci.content_body, ''))
               @@ plainto_tsquery('english', $1)
        LIMIT  $3
    """
    # Pass 1: full query (strict AND)
    rows = await conn.fetch(_SQL, query, _ALLOWED_SURFACE_KEYS, limit)
    if rows:
        return [dict(r) for r in rows]

    # Pass 2: 4-word key nouns (better recall for long rewritten queries)
    kw4 = _keyword_extract(query, max_words=4)
    if kw4 != query:
        log.debug("chat_retrieval: tsvector pass-2 kw4=%r", kw4)
        rows = await conn.fetch(_SQL, kw4, _ALLOWED_SURFACE_KEYS, limit)
        if rows:
            return [dict(r) for r in rows]

    # Pass 3: 2-word key nouns (highest recall; very sparse content needs this)
    kw2 = _keyword_extract(query, max_words=2)
    if kw2 != kw4:
        log.debug("chat_retrieval: tsvector pass-3 kw2=%r", kw2)
        rows = await conn.fetch(_SQL, kw2, _ALLOWED_SURFACE_KEYS, limit)

    return [dict(r) for r in rows]


async def _crawl_single(query: str, limit: int = 10) -> tuple[list[dict], bool]:
    """Run one crawl DB query. Returns (results, degraded).

    degraded=True means embedding failed and keyword fallback was used.
    Returns ([], False) if crawl pool is unavailable.
    """
    crawl_pool = _db.get_crawl_pool()
    if crawl_pool is None:
        return [], False

    async with crawl_pool.acquire() as conn:
        # Try vector search first
        try:
            vec = await asyncio.to_thread(_embed_sync, query)
            vec_str = _vec_to_pg(vec)
            results = await _crawl_query_vector(conn, vec_str, limit)
            return results, False
        except Exception as exc:
            log.warning(
                "chat_retrieval: fastembed failed, falling back to tsvector — %s", exc
            )

        # Keyword fallback
        try:
            results = await _crawl_query_keyword(conn, query, limit)
            return results, True
        except Exception as exc:
            log.warning("chat_retrieval: tsvector fallback also failed — %s", exc)
            return [], True


async def retrieve_crawl(
    rewritten_query: str,
    sub_queries: list[str],
) -> dict[str, Any]:
    """Phase 2: crawl DB vector search + Phase 3: sub-query multi-step retrieval.

    Returns:
        { "results": list[dict], "degraded": bool }
    """
    primary_results, degraded = await _crawl_single(rewritten_query, limit=10)

    # Phase 3: multi-step sub-query retrieval
    if sub_queries:
        extra_batches = await asyncio.gather(
            *[_crawl_single(q, limit=5) for q in sub_queries[:3]],
            return_exceptions=True,
        )
        seen_urls = {r["url"] for r in primary_results}
        for batch in extra_batches:
            if isinstance(batch, Exception):
                log.warning("chat_retrieval: sub-query crawl error — %s", batch)
                continue
            results_batch, batch_degraded = batch
            degraded = degraded or batch_degraded
            for r in results_batch:
                if r["url"] not in seen_urls:
                    primary_results.append(r)
                    seen_urls.add(r["url"])

    return {"results": primary_results, "degraded": degraded}


# ── Live search retrieval (Stage 2B) ──────────────────────────────────────────

async def retrieve_live(query: str, audience: str = "parent") -> dict[str, Any]:
    """POST to search service /api/chat-search.

    Returns {"results": [], "sites_attempted": 0} on timeout / 5xx / any exception.
    """
    if not _SEARCH_SERVICE_URL:
        log.warning("chat_retrieval: SEARCH_SERVICE_URL not set — live search skipped")
        return {"results": [], "sites_attempted": 0}

    url = f"{_SEARCH_SERVICE_URL}/api/chat-search"
    payload = {"query": query, "limit": 5, "audience": audience}

    try:
        async with httpx.AsyncClient(timeout=_LIVE_TIMEOUT, verify=False) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            sites_attempted = data.get("sites_attempted", 0)
            search_query = data.get("search_query")
            log.info(
                "chat_retrieval: live search ok results=%d sites_attempted=%d search_query=%r",
                len(results), sites_attempted, search_query,
            )
            return {"results": results, "sites_attempted": sites_attempted, "search_query": search_query}
    except httpx.TimeoutException:
        log.warning("chat_retrieval: live search timed out after %.1fs", _LIVE_TIMEOUT)
    except httpx.HTTPStatusError as exc:
        log.warning(
            "chat_retrieval: live search returned %s — %s", exc.response.status_code, exc
        )
    except Exception as exc:
        log.warning("chat_retrieval: live search error — %s", exc)

    return {"results": [], "sites_attempted": 0}


# ── Context builder ───────────────────────────────────────────────────────────

def build_context(
    log_result: dict[str, Any],
    crawl_result: dict[str, Any],
    live_result: dict[str, Any],
    recent_messages: list[dict[str, str]],
    language: str = "en",
    audience: str = "parent",
) -> dict[str, Any]:
    """Assemble the context dict passed to the Claude prompt.

    Logs are PRIMARY SIGNAL. Evidence from crawl + live supplements logs.
    source_name from each crawl/live result is preserved for citation.
    live_result: {"results": [...], "sites_attempted": int}
    """
    return {
        "child_context": {
            "trigger_counts":   log_result["trigger_counts"],
            "avg_severity":     log_result["avg_severity"],
            "peak_time_of_day": log_result["peak_time_of_day"],
            "co_occurrences":   log_result["co_occurrences"],
            "total_events":     log_result["total_events"],
        },
        "evidence": {
            "logs":    log_result["logs"][:20],          # cap to keep prompt size reasonable
            "crawled": crawl_result.get("results", [])[:10],
            "live":    live_result.get("results", [])[:5],
        },
        "conversation": recent_messages,
        "language": language,
        "audience": audience,
    }
