"""
POST /transcribe-and-log

Accepts an audio blob, transcribes it with Whisper, extracts structured
fields via LLM, conditionally saves to mzhu_test_logs and/or
mzhu_test_daily_checks, and returns the saved entry details plus
pre-fill data for the UI.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from db import get_pool
from models import MappedFields, TranscribeAndLogResponse

log = logging.getLogger(__name__)

router = APIRouter(tags=["transcribe-and-log"])

KNOWN_TRIGGERS = frozenset({
    "noise", "transitions", "sleep", "food", "social",
    "screens", "routine-change", "other",
})

KNOWN_TAGS = frozenset({
    "public_place", "sensory", "home", "school",
    "evening", "morning", "after-therapy",
})

RATING_KEYS = frozenset({
    "sleep_quality", "mood", "sensory_sensitivity", "appetite",
    "social_tolerance", "meltdown_count", "routine_adherence",
    "communication_ease", "physical_activity", "caregiver_rating",
})

EXTRACTION_SYSTEM_PROMPT = """\
You are an assistant that extracts structured data from caregiver spoken notes about a child's day.

Extract ONLY the fields that are explicitly mentioned. Return null for fields not mentioned. \
Do not infer or guess values not stated.

Return a single JSON object with exactly these fields:
{
  "event": string or null,
  "triggers": array of strings (only from: noise, transitions, sleep, food, social, screens, routine-change, other),
  "context": string or null,
  "response": string or null,
  "outcome": string or null,
  "severity": integer 1-5 or null,
  "tags": array of strings (only from: public_place, sensory, home, school, evening, morning, after-therapy),
  "notes": string or null,
  "sleep_quality": integer 1-5 or null,
  "mood": integer 1-5 or null,
  "sensory_sensitivity": integer 1-5 or null,
  "appetite": integer 1-5 or null,
  "social_tolerance": integer 1-5 or null,
  "meltdown_count": integer >= 0 or null,
  "routine_adherence": integer 1-5 or null,
  "communication_ease": integer 1-5 or null,
  "physical_activity": integer 1-5 or null,
  "caregiver_rating": integer 1-5 or null,
  "checkin_notes": string or null
}

Return ONLY valid JSON. No explanation, no markdown, no code fences.\
"""


# ── Helpers ────────────────────────────────────────────────────────────────────

def _clamp_1_5(v) -> int | None:
    if v is None:
        return None
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return max(1, min(5, v))


def _clamp_meltdown(v) -> int | None:
    if v is None:
        return None
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return max(0, v)


def _validate_mapped(raw: dict) -> MappedFields:
    """Sanitise and clamp LLM output into a MappedFields instance.

    Unknown trigger / tag values are moved into notes rather than returned
    in their respective arrays, per the contract in Section 3.
    """
    # ── triggers ──────────────────────────────────────────────────────────────
    raw_triggers = raw.get("triggers") or []
    if not isinstance(raw_triggers, list):
        raw_triggers = []
    good_triggers = [t for t in raw_triggers if t in KNOWN_TRIGGERS]
    bad_triggers  = [t for t in raw_triggers if t not in KNOWN_TRIGGERS]

    # ── tags ──────────────────────────────────────────────────────────────────
    raw_tags = raw.get("tags") or []
    if not isinstance(raw_tags, list):
        raw_tags = []
    good_tags = [t for t in raw_tags if t in KNOWN_TAGS]
    bad_tags  = [t for t in raw_tags if t not in KNOWN_TAGS]

    # ── notes (append unknown values) ─────────────────────────────────────────
    notes = raw.get("notes")
    if isinstance(notes, str):
        notes = notes.strip() or None
    overflow = []
    if bad_triggers:
        overflow.append(f"triggers: {', '.join(bad_triggers)}")
    if bad_tags:
        overflow.append(f"tags: {', '.join(bad_tags)}")
    if overflow:
        extra = "; ".join(overflow)
        notes = f"{notes} [{extra}]" if notes else f"[{extra}]"

    return MappedFields(
        event=raw.get("event") or None,
        triggers=good_triggers,
        context=raw.get("context") or None,
        response=raw.get("response") or None,
        outcome=raw.get("outcome") or None,
        severity=_clamp_1_5(raw.get("severity")),
        tags=good_tags,
        notes=notes,
        sleep_quality=_clamp_1_5(raw.get("sleep_quality")),
        mood=_clamp_1_5(raw.get("mood")),
        sensory_sensitivity=_clamp_1_5(raw.get("sensory_sensitivity")),
        appetite=_clamp_1_5(raw.get("appetite")),
        social_tolerance=_clamp_1_5(raw.get("social_tolerance")),
        meltdown_count=_clamp_meltdown(raw.get("meltdown_count")),
        routine_adherence=_clamp_1_5(raw.get("routine_adherence")),
        communication_ease=_clamp_1_5(raw.get("communication_ease")),
        physical_activity=_clamp_1_5(raw.get("physical_activity")),
        caregiver_rating=_clamp_1_5(raw.get("caregiver_rating")),
        checkin_notes=raw.get("checkin_notes") or None,
    )


def _compute_confidence(mapped: MappedFields) -> str:
    """Deterministic heuristic: count non-null / non-empty mapped fields.
    high ≥ 5, medium 2–4, low 0–1.
    """
    scalars = [
        mapped.event, mapped.context, mapped.response, mapped.outcome,
        mapped.severity, mapped.notes, mapped.checkin_notes,
        mapped.sleep_quality, mapped.mood, mapped.sensory_sensitivity,
        mapped.appetite, mapped.social_tolerance, mapped.meltdown_count,
        mapped.routine_adherence, mapped.communication_ease,
        mapped.physical_activity, mapped.caregiver_rating,
    ]
    count = sum(1 for v in scalars if v is not None)
    count += 1 if mapped.triggers else 0
    count += 1 if mapped.tags else 0

    if count >= 5:
        return "high"
    if count >= 2:
        return "medium"
    return "low"


async def _save_to_db(
    conn,
    mapped: MappedFields,
    child_id: str,
    log_date: date,
) -> tuple[object, object]:
    """Conditionally INSERT into logs and UPSERT into daily_checks.
    Returns (log_id, logged_at); either may be None if the respective
    table was not written.
    """
    log_id = None
    logged_at = None

    # ── event log ─────────────────────────────────────────────────────────────
    event_present = any([
        mapped.event,
        mapped.triggers,
        mapped.context,
        mapped.response,
        mapped.outcome,
        mapped.severity is not None,
        mapped.tags,
        mapped.notes,
    ])

    if event_present:
        row = await conn.fetchrow(
            """
            INSERT INTO mzhu_test_logs
                (child_id, event, triggers, context, response,
                 outcome, severity, tags, notes, intervention_ids)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING id, logged_at
            """,
            child_id,
            mapped.event,
            mapped.triggers,
            mapped.context,
            mapped.response,
            mapped.outcome,
            mapped.severity,
            mapped.tags,
            mapped.notes,
            [],
        )
        log_id    = row["id"]
        logged_at = row["logged_at"]

    # ── daily check-in ────────────────────────────────────────────────────────
    ratings = {
        k: getattr(mapped, k)
        for k in (
            "sleep_quality", "mood", "sensory_sensitivity", "appetite",
            "social_tolerance", "meltdown_count", "routine_adherence",
            "communication_ease", "physical_activity", "caregiver_rating",
        )
        if getattr(mapped, k) is not None
    }

    if ratings or mapped.checkin_notes:
        await conn.execute(
            """
            INSERT INTO mzhu_test_daily_checks (check_date, ratings, notes)
            VALUES ($1, $2, $3)
            ON CONFLICT (check_date) DO UPDATE SET
                ratings    = mzhu_test_daily_checks.ratings || EXCLUDED.ratings,
                notes      = COALESCE(EXCLUDED.notes, mzhu_test_daily_checks.notes),
                updated_at = now()
            """,
            log_date,
            ratings,
            mapped.checkin_notes,
        )

    return log_id, logged_at


# ── Endpoint ───────────────────────────────────────────────────────────────────

@router.post("/transcribe-and-log", response_model=TranscribeAndLogResponse)
async def transcribe_and_log(
    request: Request,
    audio: UploadFile = File(...),
    child_id: str = Form("default"),
    log_date: str | None = Form(None),
):
    # ── parse log_date ────────────────────────────────────────────────────────
    if log_date:
        try:
            parsed_date = date.fromisoformat(log_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="log_date must be YYYY-MM-DD")
    else:
        parsed_date = date.today()

    # ── read audio ────────────────────────────────────────────────────────────
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=422, detail="Empty audio file")

    suffix = Path(audio.filename or "recording.webm").suffix or ".webm"

    # ── transcribe ────────────────────────────────────────────────────────────
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name

        whisper = request.app.state.whisper_model
        segments, info = whisper.transcribe(
            tmp_path,
            beam_size=5,
            vad_filter=True,
        )
        transcription = " ".join(s.text for s in segments).strip()
        log.info(
            f"Transcribed {info.duration:.1f}s → "
            f"'{transcription[:60]}{'…' if len(transcription) > 60 else ''}'"
        )
    except Exception as e:
        log.error(f"Transcription error: {e}")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    if not transcription:
        raise HTTPException(status_code=422, detail="No speech detected in audio")

    # ── LLM extraction via `claude -p` ────────────────────────────────────────
    full_prompt = f"{EXTRACTION_SYSTEM_PROMPT}\n\nCaregiver note:\n{transcription}"
    try:
        result = subprocess.run(
            [
                "claude", "-p",
                "--tools", "",
                "--system-prompt", "",
                "--disable-slash-commands",
                full_prompt,
            ],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "claude -p exited non-zero")
        raw_text = result.stdout.strip()
        # Strip markdown code fences if Claude wraps the JSON
        if raw_text.startswith("```"):
            raw_text = raw_text[raw_text.index("\n") + 1:]  # drop ```json line
            raw_text = raw_text[:raw_text.rfind("```")].strip()  # drop closing ```
        raw_dict = json.loads(raw_text)
    except json.JSONDecodeError as e:
        log.error(f"LLM returned invalid JSON: {e}\nRaw: {raw_text!r}")
        raise HTTPException(status_code=500, detail="Field extraction failed: LLM returned invalid JSON")
    except Exception as e:
        log.error(f"LLM extraction error: {e}")
        raise HTTPException(status_code=500, detail=f"Field extraction failed: {e}")

    # ── validate + clamp ──────────────────────────────────────────────────────
    mapped     = _validate_mapped(raw_dict)
    confidence = _compute_confidence(mapped)

    # ── save ──────────────────────────────────────────────────────────────────
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                log_id, logged_at = await _save_to_db(conn, mapped, child_id, parsed_date)
    except Exception as e:
        log.error(f"DB save error: {e}")
        raise HTTPException(status_code=500, detail=f"Database save failed: {e}")

    return TranscribeAndLogResponse(
        log_id=log_id,
        log_date=parsed_date,
        logged_at=logged_at,
        transcription=transcription,
        mapping_confidence=confidence,
        mapped=mapped,
    )
