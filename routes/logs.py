from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from db import get_pool
from models import (
    FieldWarning,
    LogCreate,
    LogCreateResponse,
    LogRead,
    LogsResponse,
    LogUpdate,
)
from trigger_vocab import is_known, normalize_trigger
from routes.safety_webhook import fire_safety_webhook

log = logging.getLogger(__name__)

router = APIRouter(prefix="/logs", tags=["logs"])


# ── Time-of-day / environment enrichment ──────────────────────────────────────
# Duplicated from trigger_signals.py to avoid circular import.
# Same boundary definitions — see plan Section 3, P2.1.

def _time_of_day(hour: int) -> str:
    if 6 <= hour <= 11:
        return "morning"
    if 12 <= hour <= 16:
        return "afternoon"
    if 17 <= hour <= 20:
        return "evening"
    return "night"


_LOCATION_PRIORITY = ["school", "public_place", "home"]


def _environment_from_tags(tags: list[str]) -> str | None:
    for loc in _LOCATION_PRIORITY:
        if loc in tags:
            return loc
    return None


def _enrich_log(log_read: LogRead) -> LogRead:
    """Add computed time_of_day and environment fields."""
    log_read.time_of_day = _time_of_day(log_read.logged_at.hour)
    log_read.environment = _environment_from_tags(log_read.tags)
    return log_read


def _row_to_log(row) -> LogRead:
    d = dict(row)
    d.pop("_total", None)
    lr = LogRead.model_validate(d)
    return _enrich_log(lr)


# ── Unknown trigger tracking ──────────────────────────────────────────────────

async def _track_unknown_triggers(conn, unknown: list[str]) -> None:
    """Upsert unknown triggers into tracking table for vocabulary review."""
    for t in unknown:
        await conn.execute(
            """
            INSERT INTO mzhu_test_unknown_triggers (trigger_text)
            VALUES ($1)
            ON CONFLICT (trigger_text) DO UPDATE SET
                count     = mzhu_test_unknown_triggers.count + 1,
                last_seen = now()
            """,
            t,
        )


# ── Trigger normalization ─────────────────────────────────────────────────────

def _normalize_triggers(raw_triggers: list[str]) -> tuple[list[str], list[FieldWarning]]:
    """Normalize triggers against vocabulary. Returns (triggers, warnings).

    Unknown triggers are kept as-is in the list (preserving user input)
    but generate warnings.
    """
    normalized = []
    warnings: list[FieldWarning] = []
    seen = set()

    for raw in raw_triggers:
        t = normalize_trigger(raw)
        if t in seen:
            continue
        seen.add(t)
        normalized.append(t)
        if not is_known(t):
            warnings.append(FieldWarning(
                field="triggers",
                message=f"Unknown trigger: {t}",
                value=t,
            ))

    return normalized, warnings


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("", response_model=LogCreateResponse, status_code=201)
async def create_log(body: LogCreate):
    # Normalize triggers and collect warnings
    normalized_triggers, warnings = _normalize_triggers(body.triggers)
    unknown = [w.value for w in warnings]

    pool = get_pool()
    async with pool.acquire() as conn:
        # Track unknown triggers for vocabulary expansion review
        if unknown:
            await _track_unknown_triggers(conn, unknown)
            log.info(f"Unknown triggers logged: {unknown}")

        row = await conn.fetchrow(
            """
            INSERT INTO mzhu_test_logs
                (child_id, logged_at, event, triggers, raw_signals, context, response,
                 outcome, severity, intervention_ids, tags, notes)
            VALUES ($1, COALESCE($2, now()), $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            RETURNING *
            """,
            body.child_id,
            body.logged_at,
            body.event,
            normalized_triggers,
            body.raw_signals or [],
            body.context,
            body.response,
            body.outcome,
            body.severity,
            body.intervention_ids,
            body.tags,
            body.notes,
        )
    # P3.1: Fire safety webhook (fire-and-forget, never blocks)
    # Pass raw event text so intent detection catches semantic safety signals
    # (e.g. "want to suicide") even if trigger extraction mapped to something else.
    fire_safety_webhook(
        child_id=body.child_id or "default",
        triggers=normalized_triggers,
        severity=body.severity,
        tags=body.tags or [],
        logged_at=body.logged_at,
        event_text=body.event or "",
    )

    return LogCreateResponse(log=_row_to_log(row), warnings=warnings)


@router.get("", response_model=LogsResponse)
async def list_logs(
    days: Annotated[int, Query(ge=1)] = 30,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_voided: bool = False,
):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *, COUNT(*) OVER() AS _total
            FROM mzhu_test_logs
            WHERE logged_at >= now() - $1 * interval '1 day'
              AND (NOT voided OR $2)
            ORDER BY logged_at DESC
            LIMIT $3
            OFFSET $4
            """,
            days,
            include_voided,
            limit,
            offset,
        )
    total = rows[0]["_total"] if rows else 0
    return LogsResponse(logs=[_row_to_log(r) for r in rows], total=total)


@router.get("/{log_id}", response_model=LogRead)
async def get_log(log_id: uuid.UUID):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM mzhu_test_logs WHERE id = $1",
            log_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Log not found")
    return _row_to_log(row)


@router.put("/{log_id}", response_model=LogRead)
async def update_log(log_id: uuid.UUID, body: LogUpdate):
    """Update a log entry. Only supplied non-None fields are written;
    existing values are preserved for omitted fields (COALESCE).
    MVP: a field cannot be explicitly cleared back to null via this endpoint."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE mzhu_test_logs SET
                child_id         = COALESCE($2,  child_id),
                logged_at        = COALESCE($3,  logged_at),
                event            = COALESCE($4,  event),
                triggers         = COALESCE($5,  triggers),
                context          = COALESCE($6,  context),
                response         = COALESCE($7,  response),
                outcome          = COALESCE($8,  outcome),
                severity         = COALESCE($9,  severity),
                intervention_ids = COALESCE($10, intervention_ids),
                tags             = COALESCE($11, tags),
                notes            = COALESCE($12, notes)
            WHERE id = $1 AND NOT voided
            RETURNING *
            """,
            log_id,
            body.child_id,
            body.logged_at,
            body.event,
            body.triggers,
            body.context,
            body.response,
            body.outcome,
            body.severity,
            body.intervention_ids,
            body.tags,
            body.notes,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Log not found")
    return _row_to_log(row)


@router.put("/{log_id}/void", response_model=LogRead)
async def void_log(log_id: uuid.UUID):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE mzhu_test_logs
            SET voided    = true,
                voided_at = COALESCE(voided_at, now())
            WHERE id = $1
            RETURNING *
            """,
            log_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Log not found")
    return _row_to_log(row)
