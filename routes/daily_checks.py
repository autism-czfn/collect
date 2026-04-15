import json
from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from db import get_pool
from models import DailyCheckCreate, DailyCheckRead, DailyChecksResponse

router = APIRouter(prefix="/daily-checks", tags=["daily-checks"])


def _row_to_check(row) -> DailyCheckRead:
    d = dict(row)
    d.pop("_total", None)
    return DailyCheckRead.model_validate(d)


@router.post("", response_model=DailyCheckRead, status_code=200)
async def create_daily_check(body: DailyCheckCreate):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO mzhu_test_daily_checks (check_date, ratings, notes)
            VALUES ($1, $2, $3)
            ON CONFLICT (check_date) DO UPDATE SET
                ratings    = EXCLUDED.ratings,
                notes      = EXCLUDED.notes,
                updated_at = now()
            RETURNING *
            """,
            body.check_date,
            json.dumps(body.ratings),
            body.notes,
        )
    return _row_to_check(row)


@router.get("", response_model=DailyChecksResponse)
async def list_daily_checks(
    days: Annotated[int, Query(ge=1)] = 30,
    limit: Annotated[int, Query(ge=1, le=365)] = 90,
):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *, COUNT(*) OVER() AS _total
            FROM mzhu_test_daily_checks
            WHERE check_date >= CURRENT_DATE - ($1 * INTERVAL '1 day')
            ORDER BY check_date DESC
            LIMIT $2
            """,
            days,
            limit,
        )
    total = rows[0]["_total"] if rows else 0
    checks = [_row_to_check(r) for r in rows]
    return DailyChecksResponse(checks=checks, total=total)


@router.get("/{check_date}", response_model=DailyCheckRead)
async def get_daily_check(check_date: date):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM mzhu_test_daily_checks WHERE check_date = $1",
            check_date,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Daily check not found")
    return _row_to_check(row)
