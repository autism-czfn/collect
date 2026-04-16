from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from db import get_pool
from models import (
    InterventionCreate,
    InterventionOutcome,
    InterventionRead,
    InterventionsResponse,
)

router = APIRouter(prefix="/interventions", tags=["interventions"])

_VALID_STATUSES = {"open", "adopted", "closed"}


def _row_to_intervention(row) -> InterventionRead:
    d = dict(row)
    d.pop("_total", None)
    return InterventionRead.model_validate(d)


@router.post("", response_model=InterventionRead, status_code=201)
async def create_intervention(body: InterventionCreate):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO mzhu_test_interventions (suggestion_text, category)
            VALUES ($1, $2)
            RETURNING *
            """,
            body.suggestion_text,
            body.category,
        )
    return _row_to_intervention(row)


@router.get("", response_model=InterventionsResponse)
async def list_interventions(
    status: Annotated[str | None, Query()] = None,
    include_voided: bool = False,
):
    if status is not None and status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {sorted(_VALID_STATUSES)}",
        )
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *, COUNT(*) OVER() AS _total
            FROM mzhu_test_interventions
            WHERE ($1::text IS NULL OR status = $1)
              AND (NOT voided OR $2)
            ORDER BY suggested_at DESC
            """,
            status,
            include_voided,
        )
    total = rows[0]["_total"] if rows else 0
    return InterventionsResponse(
        interventions=[_row_to_intervention(r) for r in rows],
        total=total,
    )


@router.put("/{intervention_id}/adopt", response_model=InterventionRead)
async def adopt_intervention(intervention_id: uuid.UUID):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE mzhu_test_interventions
            SET started_at = now(),
                status     = 'adopted'
            WHERE id = $1 AND status = 'open'
            RETURNING *
            """,
            intervention_id,
        )
        if row is None:
            existing = await conn.fetchrow(
                "SELECT status FROM mzhu_test_interventions WHERE id = $1",
                intervention_id,
            )
            if existing is None:
                raise HTTPException(status_code=404, detail="Intervention not found")
            raise HTTPException(
                status_code=409,
                detail=f"Cannot adopt an intervention with status '{existing['status']}'",
            )
    return _row_to_intervention(row)


@router.put("/{intervention_id}/outcome", response_model=InterventionRead)
async def close_intervention(intervention_id: uuid.UUID, body: InterventionOutcome):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE mzhu_test_interventions
            SET closed_at    = now(),
                status       = 'closed',
                outcome_note = $2
            WHERE id = $1
            RETURNING *
            """,
            intervention_id,
            body.outcome_note,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Intervention not found")
    return _row_to_intervention(row)


@router.put("/{intervention_id}/void", response_model=InterventionRead)
async def void_intervention(intervention_id: uuid.UUID):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE mzhu_test_interventions
            SET voided    = true,
                voided_at = COALESCE(voided_at, now())
            WHERE id = $1
            RETURNING *
            """,
            intervention_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Intervention not found")
    return _row_to_intervention(row)
