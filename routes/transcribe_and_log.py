"""
POST /transcribe-and-log  (P-COL-5 / P-UI-3)

Accepts an audio blob, transcribes it with Whisper, extracts structured
fields via LLM, and returns the extraction result for caregiver review.

DECISION LOCKED — EXTRACT ONLY: this endpoint does NOT write to the DB.
After the caregiver reviews and edits the extracted fields, the UI must
POST confirmed data to POST /logs to persist the record.

Rate limited: max 10 requests per 60 seconds per client IP (P-COL-5).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from models import (
    ConfidenceScores,
    ExtractedFields,
    MappedFields,
    TranscribeAndLogResponse,
)
from trigger_vocab import CANONICAL_TRIGGERS, normalize_trigger, is_known

log = logging.getLogger(__name__)

router = APIRouter(tags=["transcribe-and-log"])

# ── Rate limiter (in-process sliding window, P-COL-5) ─────────────────────────
_RATE_LIMIT_REQUESTS = 10   # max requests per window
_RATE_LIMIT_WINDOW   = 60   # seconds
_rate_timestamps: dict[str, list[float]] = defaultdict(list)


async def _transcribe_rate_limit(request: Request) -> None:
    """Dependency: enforce per-IP rate limit on /transcribe-and-log."""
    client_ip = request.client.host if request.client else "unknown"
    now        = time.monotonic()
    cutoff     = now - _RATE_LIMIT_WINDOW

    timestamps = _rate_timestamps[client_ip]
    # Evict timestamps outside the current window
    timestamps[:] = [t for t in timestamps if t > cutoff]

    if len(timestamps) >= _RATE_LIMIT_REQUESTS:
        retry_after = int(_RATE_LIMIT_WINDOW - (now - timestamps[0])) + 1
        log.warning("Rate limit hit for IP %s (%d reqs in %ds)",
                    client_ip, len(timestamps), _RATE_LIMIT_WINDOW)
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: max {_RATE_LIMIT_REQUESTS} requests "
                   f"per {_RATE_LIMIT_WINDOW}s. Retry after {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )

    timestamps.append(now)

KNOWN_TAGS = frozenset({
    "public_place", "sensory", "home", "school",
    "evening", "morning", "after-therapy",
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
    """Sanitise and clamp LLM output into a MappedFields instance."""
    # ── triggers ──────────────────────────────────────────────────────────────
    raw_triggers = raw.get("triggers") or []
    if not isinstance(raw_triggers, list):
        raw_triggers = []
    normalized = [normalize_trigger(t) for t in raw_triggers]
    good_triggers = [t for t in normalized if is_known(t)]
    bad_triggers  = [t for t in normalized if not is_known(t)]

    # ── severity-trigger consistency: scan event/context for missed signals ──
    event_text = ((raw.get("event") or "") + " " + (raw.get("context") or "")).lower()
    _AGGRESSION_HINTS = {"暴力", "打架", "打人", "攻击", "攻擊", "咬人", "踢人",
                         "fight", "hit", "kick", "bite", "violen", "aggress"}
    _SELF_HARM_HINTS  = {"自伤", "自傷", "自残", "自殘", "撞头",
                         "self harm", "self-harm", "self injury", "head bang"}
    _ELOPEMENT_HINTS  = {"走失", "跑掉", "bolting", "ran away", "running away", "elope"}

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


def _compute_confidence(mapped: MappedFields) -> ConfidenceScores:
    """Heuristic confidence scores based on extraction completeness."""
    # trigger: high if trigger extracted with raw_signals, medium without, low if absent
    if mapped.triggers and mapped.raw_signals:
        trigger_conf = 0.9
    elif mapped.triggers:
        trigger_conf = 0.6
    else:
        trigger_conf = 0.1

    # severity: high if explicitly stated, low if absent
    sev_conf = 0.9 if mapped.severity is not None else 0.2

    overall = round((trigger_conf + sev_conf) / 2, 2)
    return ConfidenceScores(trigger=trigger_conf, severity=sev_conf, overall=overall)


def _build_extracted(mapped: MappedFields) -> ExtractedFields:
    """Map internal MappedFields to the API-facing ExtractedFields."""
    return ExtractedFields(
        trigger_type=mapped.triggers[0] if mapped.triggers else None,
        severity=mapped.severity,
        context=mapped.context,
        outcome_hint=mapped.outcome,
        tags=mapped.tags,
    )


def _build_warnings(mapped: MappedFields) -> list[str]:
    warnings = []
    if not mapped.triggers:
        warnings.append("No trigger detected — please review and select manually")
    if mapped.severity is None:
        warnings.append("No severity detected — please set severity (1–5)")
    return warnings


# ── Endpoint ───────────────────────────────────────────────────────────────────

@router.post("/transcribe-and-log", response_model=TranscribeAndLogResponse,
             dependencies=[Depends(_transcribe_rate_limit)])
async def transcribe_and_log(
    request: Request,
    audio: UploadFile = File(...),
):
    """Transcribe audio and extract behavioral fields for caregiver review.

    Does NOT write to the database. After review, the UI POSTs confirmed
    data to POST /logs to persist the record (P-UI-3 confirmation flow).
    """
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
            "Transcribed %.1fs → '%s%s'",
            info.duration,
            transcription[:60],
            "…" if len(transcription) > 60 else "",
        )
    except Exception as e:
        log.error("Transcription error: %s", e)
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    if not transcription:
        raise HTTPException(status_code=422, detail="No speech detected in audio")

    # ── LLM extraction via `claude -p` ────────────────────────────────────────
    allowed_triggers = sorted(CANONICAL_TRIGGERS)
    full_prompt = f"{EXTRACTION_SYSTEM_PROMPT}\n\nCaregiver note:\n{transcription}"

    try:
        result = subprocess.run(
            ["claude", "-p", "--disable-slash-commands", full_prompt],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "claude -p exited non-zero")

        raw_text = result.stdout.strip()
        # Strip markdown code fences if present
        if "```" in raw_text:
            fence_start = raw_text.index("```")
            after_fence = raw_text[fence_start + 3:]
            if "\n" in after_fence:
                after_fence = after_fence[after_fence.index("\n") + 1:]
            if "```" in after_fence:
                after_fence = after_fence[:after_fence.rfind("```")].strip()
            raw_text = after_fence
        else:
            brace_start = raw_text.find("{")
            brace_end   = raw_text.rfind("}")
            if brace_start != -1 and brace_end != -1:
                raw_text = raw_text[brace_start:brace_end + 1]

        raw_dict = json.loads(raw_text)

    except (json.JSONDecodeError, RuntimeError, Exception) as e:
        log.error("LLM extraction failed: %s", e)
        # Return 200 with null extraction — caregiver can enter fields manually
        return TranscribeAndLogResponse(
            raw_text=transcription,
            extracted=None,
            confidence=ConfidenceScores(trigger=0.0, severity=0.0, overall=0.0),
            allowed_trigger_values=allowed_triggers,
            warnings=["Extraction failed — please enter fields manually"],
        )

    # ── validate + build response ─────────────────────────────────────────────
    mapped   = _validate_mapped(raw_dict)
    extracted = _build_extracted(mapped)
    confidence = _compute_confidence(mapped)
    warnings   = _build_warnings(mapped)

    return TranscribeAndLogResponse(
        raw_text=transcription,
        extracted=extracted,
        confidence=confidence,
        allowed_trigger_values=allowed_triggers,
        warnings=warnings,
    )
