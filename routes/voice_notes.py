"""
Voice note management endpoints.

Note creation happens via POST /transcribe-and-log?mode=raw (see transcribe_and_log.py).
These endpoints handle read, user-edit, and soft-delete.

GET    /voice-notes           list voice notes (query: child_id, date, days)
PATCH  /voice-notes/{id}      user correction of transcribed text
DELETE /voice-notes/{id}      soft delete (void)
"""
from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from db import get_pool
from models import VoiceNoteRead, VoiceNotesResponse

log = logging.getLogger(__name__)

router = APIRouter(prefix="/voice-notes", tags=["voice-notes"])


def _row_to_note(row) -> VoiceNoteRead:
    d = dict(row)
    d.pop("_total", None)
    for field in ("sentences", "preliminary_category"):
        if d.get(field) is None:
            d[field] = []
    return VoiceNoteRead.model_validate(d)


# ── List ───────────────────────────────────────────────────────────────────────

@router.get("", response_model=VoiceNotesResponse)
async def list_voice_notes(
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
                SELECT *, COUNT(*) OVER() AS _total
                FROM mzhu_test_voice_notes
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
                SELECT *, COUNT(*) OVER() AS _total
                FROM mzhu_test_voice_notes
                WHERE child_id = $1
                  AND logged_at >= now() - $2 * interval '1 day'
                  AND (NOT voided OR $3)
                ORDER BY logged_at DESC
                LIMIT $4
                """,
                child_id, days, include_voided, limit,
            )
    total = rows[0]["_total"] if rows else 0
    return VoiceNotesResponse(notes=[_row_to_note(r) for r in rows], total=total)


# ── Patch (user correction) ────────────────────────────────────────────────────

class VoiceNotePatchBody(BaseModel):
    user_edited_text: str


@router.patch("/{note_id}", response_model=VoiceNoteRead)
async def patch_voice_note(note_id: uuid.UUID, body: VoiceNotePatchBody):
    """
    Correct a transcribed voice note.

    Writes user_edited_text and user_edited_at = now().
    The edited text is stored alongside (not replacing) raw_text so the
    original Whisper output is always preserved for audit.
    """
    edited = body.user_edited_text.strip()
    if not edited:
        raise HTTPException(status_code=422, detail="user_edited_text cannot be blank")

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE mzhu_test_voice_notes
            SET user_edited_text = $2,
                user_edited_at   = now()
            WHERE id = $1 AND NOT voided
            RETURNING *
            """,
            note_id,
            edited,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Voice note not found")
    log.info(f"voice_note patched: id={note_id}")
    return _row_to_note(row)


# ── Delete (void) ─────────────────────────────────────────────────────────────

@router.delete("/{note_id}", status_code=204)
async def void_voice_note(note_id: uuid.UUID):
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE mzhu_test_voice_notes
            SET voided = TRUE, voided_at = COALESCE(voided_at, now())
            WHERE id = $1 AND NOT voided
            """,
            note_id,
        )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Voice note not found")
    log.info(f"voice_note voided: id={note_id}")
