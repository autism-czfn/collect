"""
Food photo logging endpoints.

POST   /food-log               upload photo → nutrition analysis
GET    /food-log               list food logs (query: child_id, date)
PATCH  /food-log/{id}          user correction (foods, meal_type, notes)
DELETE /food-log/{id}          soft delete (void)
GET    /food-log/{id}/photo    retrieve raw photo bytes
"""
from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response

from claude_vision import analyze_food_photo
from db import get_pool
from models import FoodLogPatch, FoodLogRead, FoodLogsResponse
from time_utils import meal_type_from_hour

log = logging.getLogger(__name__)

router = APIRouter(prefix="/food-log", tags=["food-log"])

# Supported MIME types for phone cameras
_ALLOWED_MIME = {
    "image/jpeg", "image/jpg", "image/png", "image/heic",
    "image/heif", "image/webp",
}


def _row_to_food_log(row) -> FoodLogRead:
    d = dict(row)
    d.pop("photo_data", None)   # never expose bytes in list/patch responses
    d.pop("_total", None)
    if d.get("foods_identified") is None:
        d["foods_identified"] = []
    if d.get("macros") is None:
        d["macros"] = {}
    return FoodLogRead.model_validate(d)


# ── Create ─────────────────────────────────────────────────────────────────────

@router.post("", response_model=FoodLogRead, status_code=201)
async def create_food_log(
    request: Request,
    photo: UploadFile = File(...),
    child_id: str = Form("default"),
    client_local_hour: int | None = Form(None),
    notes: str | None = Form(None),
):
    """Upload a meal photo, analyse nutrition via Claude vision, persist the result."""
    # ── validate input ────────────────────────────────────────────────────────
    image_bytes = await photo.read()
    if not image_bytes:
        raise HTTPException(status_code=422, detail="Empty photo file")

    mime = (photo.content_type or "image/jpeg").lower()
    if mime not in _ALLOWED_MIME:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported image type '{mime}'. Accepted: jpeg, png, heic, webp",
        )

    if client_local_hour is not None and not (0 <= client_local_hour <= 23):
        raise HTTPException(status_code=422, detail="client_local_hour must be 0–23")

    # ── derive meal_type ──────────────────────────────────────────────────────
    if client_local_hour is not None:
        mt = meal_type_from_hour(client_local_hour)
    else:
        log.warning("client_local_hour not provided — meal_type will be NULL")
        mt = None

    # ── call Claude vision ────────────────────────────────────────────────────
    try:
        nutrition = analyze_food_photo(image_bytes, mime)
    except Exception as e:
        log.error(f"Vision analysis error: {e}")
        raise HTTPException(status_code=500, detail=f"Nutrition analysis failed: {e}")

    # ── persist ───────────────────────────────────────────────────────────────
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO mzhu_test_food_logs
                    (child_id, client_local_hour, meal_type, photo_data, photo_mime,
                     foods_identified, estimated_calories, macros,
                     sensory_notes, concerns, confidence, user_notes)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                RETURNING
                    id, child_id, logged_at, client_local_hour, meal_type,
                    photo_mime, foods_identified, estimated_calories, macros,
                    sensory_notes, concerns, confidence, user_notes,
                    voided, voided_at
                """,
                child_id,
                client_local_hour,
                mt,
                image_bytes,
                mime,
                nutrition.get("foods_identified") or [],
                nutrition.get("estimated_calories"),
                nutrition.get("macros") or {},
                nutrition.get("sensory_notes"),
                nutrition.get("concerns"),
                nutrition.get("confidence"),
                notes,
            )
    except Exception as e:
        log.error(f"DB save error (food_log): {e}")
        raise HTTPException(status_code=500, detail=f"Database save failed: {e}")

    log.info(
        f"food_log saved: id={row['id']} child={child_id} "
        f"meal={mt} foods={nutrition.get('foods_identified')} "
        f"cal={nutrition.get('estimated_calories')} conf={nutrition.get('confidence')}"
    )
    return _row_to_food_log(row)


# ── List ───────────────────────────────────────────────────────────────────────

@router.get("", response_model=FoodLogsResponse)
async def list_food_logs(
    child_id: str = Query("default"),
    log_date: date | None = Query(None),
    days: Annotated[int, Query(ge=1)] = 30,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    include_voided: bool = False,
):
    pool = get_pool()
    async with pool.acquire() as conn:
        if log_date:
            rows = await conn.fetch(
                """
                SELECT id, child_id, logged_at, client_local_hour, meal_type,
                       photo_mime, foods_identified, estimated_calories, macros,
                       sensory_notes, concerns, confidence, user_notes,
                       voided, voided_at,
                       COUNT(*) OVER() AS _total
                FROM mzhu_test_food_logs
                WHERE child_id = $1
                  AND logged_at::date = $2
                  AND (NOT voided OR $3)
                ORDER BY logged_at ASC
                LIMIT $4
                """,
                child_id, log_date, include_voided, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, child_id, logged_at, client_local_hour, meal_type,
                       photo_mime, foods_identified, estimated_calories, macros,
                       sensory_notes, concerns, confidence, user_notes,
                       voided, voided_at,
                       COUNT(*) OVER() AS _total
                FROM mzhu_test_food_logs
                WHERE child_id = $1
                  AND logged_at >= now() - $2 * interval '1 day'
                  AND (NOT voided OR $3)
                ORDER BY logged_at DESC
                LIMIT $4
                """,
                child_id, days, include_voided, limit,
            )
    total = rows[0]["_total"] if rows else 0
    return FoodLogsResponse(logs=[_row_to_food_log(r) for r in rows], total=total)


# ── Get photo ─────────────────────────────────────────────────────────────────

@router.get("/{food_log_id}/photo")
async def get_food_photo(food_log_id: uuid.UUID):
    """Return the raw photo bytes for a food log entry."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT photo_data, photo_mime FROM mzhu_test_food_logs WHERE id = $1 AND NOT voided",
            food_log_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Food log not found")
    if not row["photo_data"]:
        raise HTTPException(status_code=404, detail="No photo stored for this entry")
    return Response(content=bytes(row["photo_data"]), media_type=row["photo_mime"])


# ── Patch ─────────────────────────────────────────────────────────────────────

@router.patch("/{food_log_id}", response_model=FoodLogRead)
async def patch_food_log(food_log_id: uuid.UUID, body: FoodLogPatch):
    """User correction — COALESCE semantics, only supplied fields are updated."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE mzhu_test_food_logs SET
                foods_identified = COALESCE($2, foods_identified),
                meal_type        = COALESCE($3, meal_type),
                user_notes       = COALESCE($4, user_notes)
            WHERE id = $1 AND NOT voided
            RETURNING
                id, child_id, logged_at, client_local_hour, meal_type,
                photo_mime, foods_identified, estimated_calories, macros,
                sensory_notes, concerns, confidence, user_notes,
                voided, voided_at
            """,
            food_log_id,
            body.foods_identified,
            body.meal_type,
            body.user_notes,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Food log not found")
    log.info(f"food_log patched: id={food_log_id}")
    return _row_to_food_log(row)


# ── Delete (void) ─────────────────────────────────────────────────────────────

@router.delete("/{food_log_id}", status_code=204)
async def void_food_log(food_log_id: uuid.UUID):
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE mzhu_test_food_logs
            SET voided = TRUE, voided_at = COALESCE(voided_at, now())
            WHERE id = $1 AND NOT voided
            """,
            food_log_id,
        )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Food log not found")
    log.info(f"food_log voided: id={food_log_id}")
