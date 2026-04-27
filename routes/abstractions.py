"""
Daily activity abstraction endpoints.

POST /abstractions/generate              build LLM digest for a date
POST /abstractions/regenerate            re-build (archives previous version)
GET  /abstractions/{child_id}/{date}     retrieve current abstraction
PATCH /abstractions/{abstraction_id}     user corrections (allowlisted paths)
"""
from __future__ import annotations

import json
import logging
import subprocess
import uuid
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException

from db import get_pool
from models import AbstractionGenerateRequest, AbstractionPatchRequest, AbstractionRead

log = logging.getLogger(__name__)

router = APIRouter(prefix="/abstractions", tags=["abstractions"])

# ── Meltdown trigger set ───────────────────────────────────────────────────────
# Used to count incident-type events when daily_checks.meltdown_count is absent.
_MELTDOWN_TRIGGERS = frozenset({"aggression", "self_harm", "elopement"})

# ── Allowed patch paths ────────────────────────────────────────────────────────
# Keys are dot-notation paths into the `categories` JSONB.
# "llm_summary" is a special case → updates the llm_summary TEXT column directly.
ALLOWED_PATHS: frozenset[str] = frozenset({
    "overall_day_quality",
    "llm_summary",
    "meltdowns.count",
    "meltdowns.triggers",
    "meltdowns.severity",
    "meltdowns.notes",
    "social_interactions.occurred",
    "social_interactions.with_whom",
    "social_interactions.quality",
    "social_interactions.notes",
    "sensory_events.occurred",
    "sensory_events.types",
    "sensory_events.impact",
    "sensory_events.notes",
    "communication.notable_moments",
    "communication.regression_noted",
    "communication.new_achievement",
    "communication.notes",
    "routine_adherence.level",
    "routine_adherence.disruptions",
    "routine_adherence.notes",
    "mood_arc.morning",
    "mood_arc.afternoon",
    "mood_arc.evening",
    "mood_arc.overall",
    "nutrition.foods_summary",
    "nutrition.sensory_concerns",
    "nutrition.appetite_level",
    "physical_activity.occurred",
    "physical_activity.types",
    "physical_activity.notes",
    "caregiver_observations",
})

# ── LLM prompt ────────────────────────────────────────────────────────────────

ABSTRACTION_SYSTEM_PROMPT = """\
You are an autism support specialist reviewing a day's log for a child.
Given the raw data below, classify the day's key activities into autism-relevant categories.

IMPORTANT: meltdowns.count and nutrition.meals_logged are PRE-COMPUTED and will be
injected by the application after you respond. Set them to 0 — the application overwrites them.

Return ONLY a single valid JSON object with this exact schema. No markdown, no explanation.

{
  "overall_day_quality": "good" | "mixed" | "difficult" | "unknown",
  "meltdowns": {
    "count": 0,
    "triggers": ["string"],
    "severity": "mild" | "moderate" | "severe" | "none",
    "notes": "string or null"
  },
  "social_interactions": {
    "occurred": true | false,
    "with_whom": ["string"],
    "quality": "positive" | "neutral" | "negative" | "mixed",
    "notes": "string or null"
  },
  "sensory_events": {
    "occurred": true | false,
    "types": ["string"],
    "impact": "positive" | "negative" | "neutral",
    "notes": "string or null"
  },
  "communication": {
    "notable_moments": ["string"],
    "regression_noted": true | false,
    "new_achievement": true | false,
    "notes": "string or null"
  },
  "routine_adherence": {
    "level": "high" | "medium" | "low" | "unknown",
    "disruptions": ["string"],
    "notes": "string or null"
  },
  "mood_arc": {
    "morning": "positive" | "neutral" | "negative" | "unknown",
    "afternoon": "positive" | "neutral" | "negative" | "unknown",
    "evening": "positive" | "neutral" | "negative" | "unknown",
    "overall": "positive" | "neutral" | "negative" | "mixed"
  },
  "nutrition": {
    "meals_logged": 0,
    "foods_summary": ["string"],
    "sensory_concerns": "string or null",
    "appetite_level": "good" | "fair" | "poor" | "unknown"
  },
  "physical_activity": {
    "occurred": true | false,
    "types": ["string"],
    "notes": "string or null"
  },
  "caregiver_observations": ["string"],
  "llm_summary": "string"
}

Rules:
- Never invent data not present in the input.
- If a category has no data, use false / "unknown" / [] / null as defaults.
- caregiver_observations: up to 5 notable quoted phrases from voice notes or event logs.
- llm_summary: 2–3 sentences in warm, caregiver-appropriate language.
- Output ONLY the JSON object.\
"""


# ── Context document builder ───────────────────────────────────────────────────

def _build_context(
    log_date: date,
    event_logs: list[dict],
    daily_check: dict | None,
    voice_notes: list[dict],
    food_logs: list[dict],
    meals_logged: int,
    meltdown_event_count: int,
) -> str:
    lines: list[str] = [f"DATE: {log_date}"]

    # Event logs
    lines.append(f"\n=== EVENT LOGS ({len(event_logs)} records) ===")
    for i, ev in enumerate(event_logs, 1):
        ts = ev["logged_at"].strftime("%H:%M") if ev.get("logged_at") else "?"
        parts = [f"[{i}] {ts}"]
        if ev.get("event"):
            parts.append(f"event: {ev['event'][:300]}")
        if ev.get("triggers"):
            parts.append(f"triggers: {', '.join(ev['triggers'])}")
        if ev.get("severity") is not None:
            parts.append(f"severity: {ev['severity']}/5")
        if ev.get("tags"):
            parts.append(f"tags: {', '.join(ev['tags'])}")
        lines.append(" | ".join(parts))
        for field in ("context", "response", "outcome", "notes"):
            if ev.get(field):
                lines.append(f"    {field}: {ev[field][:200]}")

    # Daily check-in
    lines.append("\n=== DAILY CHECK-IN ===")
    if daily_check:
        ratings = daily_check.get("ratings") or {}
        if ratings:
            lines.append("  " + ", ".join(f"{k}: {v}" for k, v in ratings.items()))
        if daily_check.get("notes"):
            lines.append(f"  notes: {daily_check['notes'][:300]}")
    else:
        lines.append("  (no daily check-in recorded)")

    # Voice notes
    lines.append(f"\n=== VOICE NOTES ({len(voice_notes)} records) ===")
    for i, note in enumerate(voice_notes, 1):
        label = note.get("local_time_label") or "?"
        text = note.get("raw_text") or ""
        lines.append(f'[{i}] [{label}] "{text[:400]}"')

    # Food logs
    lines.append(f"\n=== FOOD LOGS ({len(food_logs)} records) ===")
    for i, fl in enumerate(food_logs, 1):
        mt = fl.get("meal_type") or "?"
        foods = ", ".join(fl.get("foods_identified") or []) or "unknown"
        cal = fl.get("estimated_calories")
        conf = fl.get("confidence") or "?"
        cal_str = f" | ~{cal} kcal" if cal is not None else ""
        lines.append(f"[{i}] [{mt}] foods: {foods}{cal_str} (confidence: {conf})")
        if fl.get("sensory_notes"):
            lines.append(f"    sensory: {fl['sensory_notes']}")
        if fl.get("concerns"):
            lines.append(f"    concerns: {fl['concerns']}")

    # Pre-computed metadata (authoritative)
    lines.append(
        f"\n=== PRE-COMPUTED METADATA (authoritative — do not override) ==="
    )
    lines.append(f"meals_logged={meals_logged}, meltdown_events={meltdown_event_count}")

    return "\n".join(lines)


# ── LLM call ──────────────────────────────────────────────────────────────────

def _call_abstraction_llm(context_doc: str) -> dict:
    """Call claude -p with the abstraction system prompt. Returns parsed dict."""
    full_prompt = f"{ABSTRACTION_SYSTEM_PROMPT}\n\n{context_doc}"
    try:
        result = subprocess.run(
            [
                "claude", "-p",
                "--tools", "",
                "--system-prompt", "",
                "--disable-slash-commands",
                full_prompt,
            ],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "claude -p exited non-zero")
        raw_text = result.stdout.strip()
    except subprocess.TimeoutExpired:
        raise RuntimeError("LLM abstraction timed out after 120 s")

    # Strip markdown fences (same logic as transcribe_and_log.py)
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
        brace_end = raw_text.rfind("}")
        if brace_start != -1 and brace_end != -1:
            raw_text = raw_text[brace_start:brace_end + 1]

    return json.loads(raw_text)


# ── Core generate logic (shared by /generate and /regenerate) ─────────────────

async def _generate_abstraction(child_id: str, log_date: date, pool) -> AbstractionRead:
    async with pool.acquire() as conn:
        # 1. Fetch all data for this child + date
        event_logs = [
            dict(r) for r in await conn.fetch(
                """
                SELECT id, logged_at, event, triggers, context, response,
                       outcome, severity, tags, notes
                FROM mzhu_test_logs
                WHERE child_id = $1
                  AND logged_at::date = $2
                  AND NOT voided
                ORDER BY logged_at ASC
                """,
                child_id, log_date,
            )
        ]

        daily_check_row = await conn.fetchrow(
            "SELECT ratings, notes FROM mzhu_test_daily_checks WHERE check_date = $1",
            log_date,
        )
        daily_check = dict(daily_check_row) if daily_check_row else None

        voice_note_rows = await conn.fetch(
            """
            SELECT id, local_time_label, raw_text, sentences
            FROM mzhu_test_voice_notes
            WHERE child_id = $1 AND logged_at::date = $2 AND NOT voided
            ORDER BY logged_at ASC
            """,
            child_id, log_date,
        )
        voice_notes = [dict(r) for r in voice_note_rows]

        food_log_rows = await conn.fetch(
            """
            SELECT id, meal_type, foods_identified, estimated_calories,
                   sensory_notes, concerns, confidence
            FROM mzhu_test_food_logs
            WHERE child_id = $1 AND logged_at::date = $2 AND NOT voided
            ORDER BY logged_at ASC
            """,
            child_id, log_date,
        )
        food_logs = [dict(r) for r in food_log_rows]

    # 2. Pre-compute authoritative values (Python — not delegated to LLM)
    meals_logged = len(food_logs)

    if daily_check and daily_check.get("ratings", {}).get("meltdown_count") is not None:
        meltdown_event_count = daily_check["ratings"]["meltdown_count"]
    else:
        meltdown_event_count = sum(
            1 for ev in event_logs
            if any(t in _MELTDOWN_TRIGGERS for t in (ev.get("triggers") or []))
            or (ev.get("severity") is not None and ev["severity"] >= 4)
        )

    # 3. Extract all voice note sentences verbatim (Python — never from LLM)
    all_voice_sentences: list[str] = []
    for note in voice_notes:
        sents = note.get("sentences") or []
        if sents:
            all_voice_sentences.extend(sents)
        elif note.get("raw_text"):
            # Fall back to raw_text if sentences weren't split at capture time
            all_voice_sentences.append(note["raw_text"])

    # 4. Build context document
    context_doc = _build_context(
        log_date, event_logs, daily_check, voice_notes, food_logs,
        meals_logged, meltdown_event_count,
    )

    # 5. Call LLM
    try:
        parsed = _call_abstraction_llm(context_doc)
    except json.JSONDecodeError as e:
        log.error(f"Abstraction LLM returned invalid JSON: {e}")
        raise HTTPException(status_code=500, detail="Abstraction failed: LLM returned invalid JSON")
    except Exception as e:
        log.error(f"Abstraction LLM error: {e}")
        raise HTTPException(status_code=500, detail=f"Abstraction failed: {e}")

    # 6. Extract llm_summary from parsed output, build flat categories dict
    llm_summary = parsed.pop("llm_summary", None)

    # Flatten: merge top-level fields (overall_day_quality) with sub-categories
    categories: dict[str, Any] = {}
    if "overall_day_quality" in parsed:
        categories["overall_day_quality"] = parsed["overall_day_quality"]
    # Merge all category sub-objects
    for key in (
        "meltdowns", "social_interactions", "sensory_events", "communication",
        "routine_adherence", "mood_arc", "nutrition", "physical_activity",
        "caregiver_observations",
    ):
        if key in parsed:
            categories[key] = parsed[key]

    # 7. Inject Python-computed values (override LLM placeholders)
    if "meltdowns" not in categories:
        categories["meltdowns"] = {}
    categories["meltdowns"]["count"] = meltdown_event_count

    if "nutrition" not in categories:
        categories["nutrition"] = {}
    categories["nutrition"]["meals_logged"] = meals_logged

    # 8. Build source_ids
    source_ids = {
        "log_ids": [str(ev["id"]) for ev in event_logs],
        "note_ids": [str(r["id"]) for r in voice_note_rows],
        "food_log_ids": [str(fl["id"]) for fl in food_logs],
    }

    # 9. Persist
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO mzhu_test_activity_abstractions
                    (child_id, log_date, source_ids, categories, llm_summary,
                     raw_sentences_kept, user_corrections)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
                """,
                child_id,
                log_date,
                source_ids,
                categories,
                llm_summary,
                all_voice_sentences,
                {},
            )
    except Exception as e:
        log.error(f"DB save error (abstraction): child={child_id} date={log_date} error={e}")
        raise HTTPException(status_code=500, detail=f"Database save failed: {e}")

    log.info(
        f"abstraction saved: id={row['id']} child={child_id} date={log_date} "
        f"meals={meals_logged} meltdowns={meltdown_event_count} "
        f"sentences={len(all_voice_sentences)} quality={categories.get('overall_day_quality')}"
    )
    return _row_to_abstraction(row)


def _row_to_abstraction(row) -> AbstractionRead:
    d = dict(row)
    for f in ("source_ids", "categories", "user_corrections"):
        if d.get(f) is None:
            d[f] = {}
    if d.get("raw_sentences_kept") is None:
        d["raw_sentences_kept"] = []
    return AbstractionRead.model_validate(d)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/generate", response_model=AbstractionRead, status_code=201)
async def generate_abstraction(body: AbstractionGenerateRequest):
    """
    Generate (or overwrite) the LLM activity digest for a given date.

    If an abstraction already exists for this child+date, it is archived
    (is_current=FALSE) before the new one is written.
    """
    pool = get_pool()

    # Archive any existing current abstraction for this date
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE mzhu_test_activity_abstractions
            SET is_current = FALSE
            WHERE child_id = $1 AND log_date = $2 AND is_current = TRUE
            """,
            body.child_id, body.log_date,
        )

    return await _generate_abstraction(body.child_id, body.log_date, pool)


@router.post("/regenerate", response_model=AbstractionRead, status_code=201)
async def regenerate_abstraction(body: AbstractionGenerateRequest):
    """
    Re-run the LLM pass after the user has added or corrected data.

    Archives the previous version (is_current=FALSE) then generates fresh.
    Identical to /generate in behaviour — provided as a distinct endpoint
    for semantic clarity in the frontend ("Regenerate" vs "Generate").
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE mzhu_test_activity_abstractions
            SET is_current = FALSE
            WHERE child_id = $1 AND log_date = $2 AND is_current = TRUE
            """,
            body.child_id, body.log_date,
        )
    return await _generate_abstraction(body.child_id, body.log_date, pool)


@router.get("/{child_id}/{log_date}", response_model=AbstractionRead)
async def get_abstraction(child_id: str, log_date: date):
    """Return the current (is_current=TRUE) abstraction for a child+date."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM mzhu_test_activity_abstractions
            WHERE child_id = $1 AND log_date = $2 AND is_current = TRUE
            """,
            child_id, log_date,
        )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="No abstraction found. Call POST /abstractions/generate first.",
        )
    return _row_to_abstraction(row)


@router.patch("/{abstraction_id}", response_model=AbstractionRead)
async def patch_abstraction(abstraction_id: uuid.UUID, body: AbstractionPatchRequest):
    """
    Apply user corrections to specific fields of the abstraction.

    Body: { "corrections": { "meltdowns.count": 2, "mood_arc.morning": "positive" } }

    All paths are validated against ALLOWED_PATHS. Unknown paths are rejected
    with HTTP 422. Corrections are audited in user_corrections JSONB.
    version is incremented in the same SQL statement.
    """
    if not body.corrections:
        raise HTTPException(status_code=422, detail="corrections cannot be empty")

    # Validate all paths before touching the DB
    invalid = [k for k in body.corrections if k not in ALLOWED_PATHS]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown correction path(s): {invalid}. "
                   f"Allowed: {sorted(ALLOWED_PATHS)}",
        )

    pool = get_pool()
    async with pool.acquire() as conn:
        # Read current state for audit trail and in-place modification
        row = await conn.fetchrow(
            """
            SELECT categories, llm_summary, user_corrections, version
            FROM mzhu_test_activity_abstractions
            WHERE id = $1 AND is_current = TRUE
            """,
            abstraction_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Abstraction not found")

        categories: dict[str, Any] = dict(row["categories"] or {})
        user_corrections: dict[str, Any] = dict(row["user_corrections"] or {})
        new_llm_summary: str | None = row["llm_summary"]
        corrected_at = datetime.now(timezone.utc).isoformat()

        # Apply each correction
        for dot_path, new_value in body.corrections.items():
            if dot_path == "llm_summary":
                # Special case: targets the llm_summary TEXT column directly
                original = new_llm_summary
                new_llm_summary = str(new_value) if new_value is not None else None
            else:
                # Navigate into categories dict using the dot-path
                keys = dot_path.split(".")
                original = _get_nested(categories, keys)
                _set_nested(categories, keys, new_value)

            # Record audit entry
            user_corrections[dot_path] = {
                "original": original,
                "corrected": new_value,
                "corrected_at": corrected_at,
            }

        # Write back — version = version + 1 in SQL
        updated_row = await conn.fetchrow(
            """
            UPDATE mzhu_test_activity_abstractions
            SET categories       = $2,
                llm_summary      = $3,
                user_corrections = $4,
                version          = version + 1
            WHERE id = $1 AND is_current = TRUE
            RETURNING *
            """,
            abstraction_id,
            categories,
            new_llm_summary,
            user_corrections,
        )

    if updated_row is None:
        raise HTTPException(status_code=404, detail="Abstraction not found")
    log.info(
        f"abstraction patched: id={abstraction_id} "
        f"paths={list(body.corrections.keys())} version={updated_row['version']}"
    )
    return _row_to_abstraction(updated_row)


# ── Dict path helpers ─────────────────────────────────────────────────────────

def _get_nested(data: dict, keys: list[str]) -> Any:
    """Safely retrieve a nested value; returns None if any key is missing."""
    d: Any = data
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def _set_nested(data: dict, keys: list[str], value: Any) -> None:
    """Set a nested value, creating intermediate dicts as needed."""
    d = data
    for k in keys[:-1]:
        if k not in d or not isinstance(d[k], dict):
            d[k] = {}
        d = d[k]
    d[keys[-1]] = value
