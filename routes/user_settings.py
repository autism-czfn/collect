from __future__ import annotations
"""
GET  /collect/user-settings  — retrieve caregiver settings
POST /collect/user-settings  — create or upsert caregiver settings

Mounted with no external prefix in main.py. nginx strips /collect/ before proxying,
so FastAPI exposes /user-settings and nginx reaches it via /collect/user-settings.

Key behaviours (P-COL-6):
  - GET with no existing record → HTTP 200 with all fields null (NOT 404)
  - POST uses INSERT ... ON CONFLICT DO UPDATE with COALESCE so omitted fields
    are not overwritten with null (partial update semantics)
  - PUT is NOT implemented — nginx blocks PUT (405); use POST for all writes
"""

import logging

from fastapi import APIRouter, Query

from db import get_pool
from models import UserSettingsCreate, UserSettingsRead

log = logging.getLogger(__name__)

router = APIRouter(tags=["user-settings"])


@router.get("/user-settings", response_model=UserSettingsRead)
async def get_user_settings(
    user_id: str = Query("default"),
    child_id: str = Query("default"),
):
    """Return settings for (user_id, child_id).

    Returns HTTP 200 with all fields set to null when no record exists —
    never 404. Null fields → UI renders empty default form for first-time users.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT user_id, child_id, timezone, language,
                   child_display_name, ui_preferences, updated_at
            FROM mzhu_test_user_settings
            WHERE user_id = $1 AND child_id = $2
            """,
            user_id,
            child_id,
        )

    if row is None:
        log.info("No settings found for user=%s child=%s — returning null defaults", user_id, child_id)
        return UserSettingsRead(
            user_id=user_id,
            child_id=child_id,
            timezone=None,
            language=None,
            child_display_name=None,
            ui_preferences=None,
            updated_at=None,
        )

    return UserSettingsRead.model_validate(dict(row))


@router.post("/user-settings", response_model=UserSettingsRead)
async def upsert_user_settings(body: UserSettingsCreate):
    """Create or update settings for (user_id, child_id).

    Partial updates are supported — omitted (None) fields are preserved via
    COALESCE in the ON CONFLICT DO UPDATE clause.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO mzhu_test_user_settings
                (user_id, child_id, timezone, language,
                 child_display_name, ui_preferences, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, now())
            ON CONFLICT (user_id, child_id) DO UPDATE SET
                timezone           = COALESCE(EXCLUDED.timezone,
                                              mzhu_test_user_settings.timezone),
                language           = COALESCE(EXCLUDED.language,
                                              mzhu_test_user_settings.language),
                child_display_name = COALESCE(EXCLUDED.child_display_name,
                                              mzhu_test_user_settings.child_display_name),
                ui_preferences     = COALESCE(EXCLUDED.ui_preferences,
                                              mzhu_test_user_settings.ui_preferences),
                updated_at         = now()
            RETURNING user_id, child_id, timezone, language,
                      child_display_name, ui_preferences, updated_at
            """,
            body.user_id,
            body.child_id,
            body.timezone,
            body.language,
            body.child_display_name,
            body.ui_preferences,
        )

    log.info("Settings upserted for user=%s child=%s", body.user_id, body.child_id)
    return UserSettingsRead.model_validate(dict(row))
