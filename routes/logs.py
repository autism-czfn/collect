import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from db import get_pool
from models import LogCreate, LogRead, LogsResponse, LogUpdate

router = APIRouter(prefix="/logs", tags=["logs"])


def _row_to_log(row) -> LogRead:
    d = dict(row)
    d.pop("_total", None)
    return LogRead.model_validate(d)


@router.post("", response_model=LogRead, status_code=201)
async def create_log(body: LogCreate):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO mzhu_test_logs
                (child_id, logged_at, event, triggers, context, response,
                 outcome, severity, intervention_ids, tags, notes)
            VALUES ($1, COALESCE($2, now()), $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING *
            """,
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
    return _row_to_log(row)


@router.get("", response_model=LogsResponse)
async def list_logs(
    days: Annotated[int, Query(ge=1)] = 30,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
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
            """,
            days,
            include_voided,
            limit,
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
