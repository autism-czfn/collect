"""
POST /transcribe-and-log

Accepts an audio blob, transcribes it with Whisper, then either:
  mode="structured" (default): extracts structured fields via LLM, saves to
      mzhu_test_logs and/or mzhu_test_daily_checks.
  mode="raw": stores verbatim sentences in mzhu_test_voice_notes with
      rule-based category tagging; no LLM extraction.

Returns TranscribeAndLogResponse (structured) or VoiceNoteRead (raw).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from routes.safety_webhook import fire_safety_webhook

from db import get_pool
from models import MappedFields, TranscribeAndLogResponse, VoiceNoteRead
from time_utils import time_label_from_hour
from trigger_vocab import CANONICAL_TRIGGERS, normalize_trigger, is_known

log = logging.getLogger(__name__)

router = APIRouter(tags=["transcribe-and-log"])

# KNOWN_TRIGGERS now loaded from config/triggers.json via trigger_vocab module
KNOWN_TRIGGERS = CANONICAL_TRIGGERS

# ── Raw-mode: rule-based sentence categorisation ──────────────────────────────

_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "mood": [
        "happy", "sad", "anxious", "calm", "frustrated", "excited", "angry",
        "content", "joyful", "upset", "worried", "stress", "cheerful", "grumpy",
        "irritable", "euphoric", "depressed",
    ],
    "meltdown": [
        "meltdown", "tantrum", "breakdown", "hit", "scream", "threw", "throwing",
        "kick", "bite", "biting", "aggress", "rage", "explosion", "outburst",
    ],
    "social": [
        "friend", "cousin", "sibling", "classmate", "played with", "talked to",
        "peer", "teacher", "therapist", "connect", "interact", "together",
        "share", "turn-taking",
    ],
    "sensory": [
        "loud", "bright", "texture", "smell", "refused to eat", "covered ears",
        "sensitiv", "overwhelm", "noise", "light", "touch", "scratch",
        "itchy", "temperature", "taste", "wet",
    ],
    "routine": [
        "schedule", "transition", "refused", "surprise", "change", "unexpected",
        "routine", "structure", "out of order", "different", "deviation",
    ],
    "sleep": [
        "sleep", "nap", "woke up", "nightmare", "tired", "bedtime",
        "insomnia", "drowsy", "restless", "didn't sleep",
    ],
}


def _categorize_text(text: str) -> list[str]:
    """Return sorted category tags found in text (note-level union)."""
    lower = text.lower()
    found = [
        cat for cat, kws in _CATEGORY_KEYWORDS.items()
        if any(kw in lower for kw in kws)
    ]
    return sorted(found) if found else ["other"]


def _split_sentences(text: str) -> list[str]:
    """Split transcript on sentence-ending punctuation."""
    parts = re.split(r"(?<=[.!?。！？])\s+", text)
    return [p.strip() for p in parts if p.strip()]

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

The caregiver may speak in ANY language (English, Chinese, Japanese, French, etc.). \
Regardless of input language, ALL output field values MUST be in English. \
Translate behavioral descriptions into standard English clinical/behavioral terms.

Extract ONLY the fields that are explicitly mentioned. Return null for fields not mentioned. \
Do not infer or guess values not stated.

CRITICAL — trigger mapping rules (use the most specific term that fits):
  - Fighting, hitting, kicking, biting, throwing objects, 打架, 暴力, 打人, 攻击 → "aggression"
  - Self-injury, head banging, self-biting, 自伤, 自残, 撞头 → "self_harm"
  - Suicide, suicidal, kill himself, 自杀, 想死, 不想活 → "self_harm"
  - Running away, bolting, wandering off, 走失, 跑掉 → "elopement"
  - Worry, panic, fear, 焦虑, 不安 → "anxiety"
  - Refusing demands, task avoidance → "demand_avoidance"
  - Do NOT map violence/aggression to "social". "social" is for non-violent peer interaction difficulties only.

CRITICAL — raw_signals extraction:
  For EVERY trigger you extract, ALSO extract the EXACT original phrase from the input that led to it.
  Keep the original language (Chinese, English, etc.). Do NOT translate raw_signals.
  Examples:
    Input: "孩子说不想活了" → triggers: ["self_harm"], raw_signals: ["不想活了"]
    Input: "kid wants to kill himself" → triggers: ["self_harm"], raw_signals: ["kill himself"]
    Input: "hitting teacher and ran away" → triggers: ["aggression", "elopement"], raw_signals: ["hitting teacher", "ran away"]

Return a single JSON object with exactly these fields:
{
  "event": string or null (in English),
  "triggers": array of strings (only from: noise, transitions, sleep, food, social, screens, routine_change, crowd, sensory_overload, school_stress, aggression, self_harm, elopement, anxiety, demand_avoidance, other),
  "raw_signals": array of strings (exact original phrases from input, in original language, one per trigger),
  "context": string or null (in English),
  "response": string or null (in English),
  "outcome": string or null (in English),
  "severity": integer 1-5 or null,
  "tags": array of strings (only from: public_place, sensory, home, school, evening, morning, after-therapy),
  "notes": string or null (in English),
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
  "checkin_notes": string or null (in English)
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
    # Normalize through shared vocabulary (resolve aliases, hyphen→underscore)
    normalized = [normalize_trigger(t) for t in raw_triggers]
    good_triggers = [t for t in normalized if is_known(t)]
    bad_triggers  = [t for t in normalized if not is_known(t)]

    # ── severity-trigger consistency: scan event/context for missed signals ──
    severity = raw.get("severity")
    event_text = ((raw.get("event") or "") + " " + (raw.get("context") or "")).lower()
    _AGGRESSION_HINTS = {"暴力", "打架", "打人", "攻击", "攻擊", "咬人", "踢人",
                         "fight", "hit", "kick", "bite", "violen", "aggress"}
    _SELF_HARM_HINTS = {"自伤", "自傷", "自残", "自殘", "撞头",
                        "self harm", "self-harm", "self injury", "head bang"}
    _ELOPEMENT_HINTS = {"走失", "跑掉", "bolting", "ran away", "running away", "elope"}

    if "aggression" not in good_triggers and any(h in event_text for h in _AGGRESSION_HINTS):
        good_triggers.append("aggression")
        log.info("severity-trigger fix: added 'aggression' from event/context text")
    if "self_harm" not in good_triggers and any(h in event_text for h in _SELF_HARM_HINTS):
        good_triggers.append("self_harm")
        log.info("severity-trigger fix: added 'self_harm' from event/context text")
    if "elopement" not in good_triggers and any(h in event_text for h in _ELOPEMENT_HINTS):
        good_triggers.append("elopement")
        log.info("severity-trigger fix: added 'elopement' from event/context text")

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

    # Extract raw_signals from LLM output (preserves original language)
    raw_signals = raw.get("raw_signals") or []
    if not isinstance(raw_signals, list):
        raw_signals = []
    raw_signals = [str(s).strip() for s in raw_signals if s and str(s).strip()]

    return MappedFields(
        event=raw.get("event") or None,
        triggers=good_triggers,
        raw_signals=raw_signals,
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
    transcription: str = "",
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
                (child_id, event, triggers, raw_signals, context, response,
                 outcome, severity, tags, notes, intervention_ids)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING id, logged_at
            """,
            child_id,
            mapped.event,
            mapped.triggers,
            mapped.raw_signals,
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

        # P3.1: Fire safety webhook on voice-logged events
        # Pass both the raw transcription AND event text for intent detection
        fire_safety_webhook(
            child_id=child_id,
            triggers=mapped.triggers or [],
            severity=mapped.severity,
            tags=mapped.tags or [],
            logged_at=logged_at,
            event_text=f"{transcription} {mapped.event or ''}",
        )

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

@router.post("/transcribe-and-log")
async def transcribe_and_log(
    request: Request,
    audio: UploadFile = File(...),
    child_id: str = Form("default"),
    log_date: str | None = Form(None),
    mode: str = Form("structured"),
    client_local_hour: int | None = Form(None),
):
    """
    mode="structured" (default): LLM extraction → mzhu_test_logs / mzhu_test_daily_checks.
      Returns TranscribeAndLogResponse.

    mode="raw": verbatim sentences → mzhu_test_voice_notes.
      Returns VoiceNoteRead. No LLM call.

    client_local_hour: integer 0-23 from new Date().getHours().
      Used for local_time_label in raw mode. Ignored in structured mode.
    """
    if mode not in ("structured", "raw"):
        raise HTTPException(status_code=422, detail="mode must be 'structured' or 'raw'")

    if client_local_hour is not None and not (0 <= client_local_hour <= 23):
        raise HTTPException(status_code=422, detail="client_local_hour must be 0–23")

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
            f"[{mode}] Transcribed {info.duration:.1f}s → "
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

    # ── raw mode: store verbatim sentences, skip LLM ─────────────────────────
    if mode == "raw":
        return await _handle_raw_mode(
            transcription, child_id, parsed_date, client_local_hour
        )

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
        # Strip markdown code fences if Claude wraps the JSON.
        # Handles both "```json\n{...}\n```" at the start AND cases where
        # Claude adds preamble text before the fenced block.
        if "```" in raw_text:
            # Find the first opening fence
            fence_start = raw_text.index("```")
            after_fence = raw_text[fence_start + 3:]
            # Skip the language tag line (e.g. "json\n")
            if "\n" in after_fence:
                after_fence = after_fence[after_fence.index("\n") + 1:]
            # Find the closing fence
            if "```" in after_fence:
                after_fence = after_fence[:after_fence.rfind("```")].strip()
            raw_text = after_fence
        # Handle case where Claude outputs text before/after bare JSON
        else:
            brace_start = raw_text.find("{")
            brace_end = raw_text.rfind("}")
            if brace_start != -1 and brace_end != -1:
                raw_text = raw_text[brace_start:brace_end + 1]
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
                log_id, logged_at = await _save_to_db(conn, mapped, child_id, parsed_date, transcription)

                # Option A: also archive the raw transcript in voice_notes so the
                # original words are never lost, even when structured extraction ran.
                sentences        = _split_sentences(transcription)
                categories       = _categorize_text(transcription)
                local_time_label = time_label_from_hour(client_local_hour) if client_local_hour is not None else None
                vn_row = await conn.fetchrow(
                    """
                    INSERT INTO mzhu_test_voice_notes
                        (child_id, client_local_hour, local_time_label,
                         raw_text, sentences, preliminary_category)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING id
                    """,
                    child_id,
                    client_local_hour,
                    local_time_label,
                    transcription,
                    sentences,
                    categories,
                )
                log.info(
                    f"voice_note archived (structured): id={vn_row['id']} child={child_id} "
                    f"sentences={len(sentences)} categories={categories}"
                )
    except Exception as e:
        log.error(f"DB save error: {e}")
        raise HTTPException(status_code=500, detail=f"Database save failed: {e}")

    return TranscribeAndLogResponse(
        log_id=log_id,
        log_date=parsed_date,
        logged_at=logged_at,
        raw_text=transcription,
        mapping_confidence=confidence,
        mapped=mapped,
    )


# ── Raw mode handler ───────────────────────────────────────────────────────────

async def _handle_raw_mode(
    transcription: str,
    child_id: str,
    log_date: date,
    client_local_hour: int | None,
) -> VoiceNoteRead:
    """
    Store verbatim voice note sentences without LLM extraction.

    1. Split transcription into sentences.
    2. Apply rule-based category tagging at note level (union across all sentences).
    3. Compute local_time_label from client_local_hour.
    4. INSERT into mzhu_test_voice_notes.
    5. Return VoiceNoteRead.
    """
    sentences = _split_sentences(transcription)
    categories = _categorize_text(transcription)
    local_time_label = time_label_from_hour(client_local_hour) if client_local_hour is not None else None

    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO mzhu_test_voice_notes
                    (child_id, client_local_hour, local_time_label,
                     raw_text, sentences, preliminary_category)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING *
                """,
                child_id,
                client_local_hour,
                local_time_label,
                transcription,
                sentences,
                categories,
            )
    except Exception as e:
        log.error(f"DB save error (voice_note raw): {e}")
        raise HTTPException(status_code=500, detail=f"Database save failed: {e}")

    log.info(
        f"voice_note saved: id={row['id']} child={child_id} "
        f"label={local_time_label} sentences={len(sentences)} "
        f"categories={categories}"
    )
    d = dict(row)
    for field in ("sentences", "preliminary_category"):
        if d.get(field) is None:
            d[field] = []
    return VoiceNoteRead.model_validate(d)
