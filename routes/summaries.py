from fastapi import APIRouter, HTTPException

from db import get_pool
from models import SummaryCreate, SummaryRead

router = APIRouter(prefix="/summaries", tags=["summaries"])


def _row_to_summary(row) -> SummaryRead:
    return SummaryRead.model_validate(dict(row))


@router.post("", response_model=SummaryRead, status_code=200)
async def upsert_summary(body: SummaryCreate):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO mzhu_test_summaries (week_start, summary_text, stats_json)
            VALUES ($1, $2, $3)
            ON CONFLICT (week_start) DO UPDATE
              SET summary_text = EXCLUDED.summary_text,
                  stats_json   = EXCLUDED.stats_json,
                  generated_at = now()
            RETURNING *
            """,
            body.week_start,
            body.summary_text,
            body.stats_json,
        )
    return _row_to_summary(row)


@router.get("/latest", response_model=SummaryRead)
async def get_latest_summary():
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM mzhu_test_summaries ORDER BY week_start DESC LIMIT 1"
        )
    if row is None:
        raise HTTPException(status_code=404, detail="No summaries found")
    return _row_to_summary(row)
