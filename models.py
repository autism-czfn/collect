from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── Logs ──────────────────────────────────────────────────────────────────────

class LogCreate(BaseModel):
    child_id: str | None = "default"
    logged_at: datetime | None = None          # None → server uses now()
    event: str | None = Field(None, max_length=2000)
    triggers: list[str] = Field(default_factory=list)
    raw_signals: list[str] = Field(default_factory=list)
    context: str | None = Field(None, max_length=5000)
    response: str | None = Field(None, max_length=5000)
    outcome: str | None = None
    severity: int | None = None
    intervention_ids: list[uuid.UUID] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None

    @field_validator("triggers", mode="before")
    @classmethod
    def normalise_triggers(cls, v: list) -> list[str]:
        result = []
        for item in v:
            item = str(item).strip().lower()
            if len(item) > 50:
                raise ValueError(f"Trigger item exceeds 50 chars: '{item[:20]}…'")
            result.append(item)
        return result

    @field_validator("severity")
    @classmethod
    def clamp_severity(cls, v: int | None) -> int | None:
        if v is None:
            return None
        return max(1, min(5, v))


class LogUpdate(BaseModel):
    """All fields optional — only supplied non-None fields are written.
    MVP limitation: setting a field back to null via PUT is not supported
    (COALESCE preserves the existing value when incoming is None)."""
    child_id: str | None = None
    logged_at: datetime | None = None
    event: str | None = None
    triggers: list[str] | None = None
    context: str | None = None
    response: str | None = None
    outcome: str | None = None
    severity: int | None = None
    intervention_ids: list[uuid.UUID] | None = None
    tags: list[str] | None = None
    notes: str | None = None


class FieldWarning(BaseModel):
    field: str
    message: str
    value: str


class LogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    child_id: str | None
    logged_at: datetime
    event: str | None
    triggers: list[str]
    raw_signals: list[str] = Field(default_factory=list)
    context: str | None
    response: str | None
    outcome: str | None
    severity: int | None
    intervention_ids: list[uuid.UUID]
    tags: list[str]
    notes: str | None
    voided: bool
    voided_at: datetime | None
    # Computed enrichment fields (set by API, not stored in DB)
    time_of_day: str | None = None
    environment: str | None = None


class LogCreateResponse(BaseModel):
    """POST /logs response — includes warnings for unknown triggers."""
    log: LogRead
    warnings: list[FieldWarning] = []


class LogsResponse(BaseModel):
    logs: list[LogRead]
    total: int


# ── Interventions ──────────────────────────────────────────────────────────────

class InterventionCreate(BaseModel):
    suggestion_text: str = Field(..., max_length=2000)
    category: str | None = None


class InterventionOutcome(BaseModel):
    outcome_note: str | None = Field(None, max_length=5000)
    status: Literal["closed"]


class InterventionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    suggestion_text: str
    category: str | None
    suggested_at: datetime
    started_at: datetime | None
    status: str
    outcome_note: str | None
    closed_at: datetime | None
    voided: bool
    voided_at: datetime | None


class InterventionsResponse(BaseModel):
    interventions: list[InterventionRead]
    total: int


# ── Summaries ──────────────────────────────────────────────────────────────────

class SummaryCreate(BaseModel):
    week_start: date
    summary_text: str
    stats_json: dict[str, Any]


class SummaryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    week_start: date
    summary_text: str
    stats_json: dict[str, Any]
    generated_at: datetime


# ── Daily Checks ───────────────────────────────────────────────────────────────

VALID_RATING_KEYS: frozenset[str] = frozenset({
    "sleep_quality", "mood", "sensory_sensitivity", "appetite",
    "social_tolerance", "routine_adherence", "communication_ease",
    "physical_activity", "caregiver_rating", "meltdown_count",
})

RATING_1_5_KEYS: frozenset[str] = VALID_RATING_KEYS - {"meltdown_count"}


class DailyCheckCreate(BaseModel):
    check_date: date
    ratings: dict[str, int] = Field(default_factory=dict)  # sparse — omit untouched keys
    notes: str | None = None

    @field_validator("check_date")
    @classmethod
    def not_in_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("check_date cannot be in the future")
        return v

    @field_validator("ratings", mode="before")
    @classmethod
    def validate_ratings(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            raise ValueError("ratings must be an object")
        extra = v.keys() - VALID_RATING_KEYS
        if extra:
            raise ValueError(f"ratings contains unknown keys: {sorted(extra)}")
        for key in RATING_1_5_KEYS:
            if key in v:
                val = v[key]
                if not isinstance(val, int) or not (1 <= val <= 5):
                    raise ValueError(f"ratings.{key} must be an integer between 1 and 5")
        if "meltdown_count" in v:
            mc = v["meltdown_count"]
            if not isinstance(mc, int) or mc < 0:
                raise ValueError("ratings.meltdown_count must be an integer >= 0")
        return v

    @field_validator("notes")
    @classmethod
    def notes_length(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 5000:
            raise ValueError("notes exceeds 5000 chars")
        return v


class DailyCheckRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    check_date: date
    ratings: dict[str, int]
    notes: str | None
    created_at: datetime
    updated_at: datetime


class DailyChecksResponse(BaseModel):
    checks: list[DailyCheckRead]
    total: int


# ── Transcribe-and-Log ─────────────────────────────────────────────────────────

class MappedFields(BaseModel):
    # Event log fields
    event: str | None = None
    triggers: list[str] = Field(default_factory=list)
    raw_signals: list[str] = Field(default_factory=list)
    context: str | None = None
    response: str | None = None
    outcome: str | None = None
    severity: int | None = None
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None
    # Daily rating fields
    sleep_quality: int | None = None
    mood: int | None = None
    sensory_sensitivity: int | None = None
    appetite: int | None = None
    social_tolerance: int | None = None
    meltdown_count: int | None = None
    routine_adherence: int | None = None
    communication_ease: int | None = None
    physical_activity: int | None = None
    caregiver_rating: int | None = None
    checkin_notes: str | None = None


class TranscribeAndLogResponse(BaseModel):
    log_id: uuid.UUID | None
    log_date: date
    logged_at: datetime | None          # timestamp of the saved logs row; null if no event fields
    raw_text: str
    mapping_confidence: Literal["high", "medium", "low"]
    mapped: MappedFields


# ── Food Logs ──────────────────────────────────────────────────────────────────

class FoodLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    child_id: str
    logged_at: datetime
    client_local_hour: int | None
    meal_type: str | None
    photo_mime: str
    foods_identified: list[str] = Field(default_factory=list)
    estimated_calories: int | None
    macros: dict[str, Any] = Field(default_factory=dict)
    sensory_notes: str | None
    concerns: str | None
    confidence: str | None
    user_notes: str | None
    voided: bool
    voided_at: datetime | None
    # photo_data intentionally excluded — retrieve via GET /food-log/{id}/photo


class FoodLogPatch(BaseModel):
    """All fields optional — COALESCE semantics on write."""
    foods_identified: list[str] | None = None
    meal_type: str | None = None
    user_notes: str | None = None

    @field_validator("meal_type")
    @classmethod
    def valid_meal_type(cls, v: str | None) -> str | None:
        if v is None:
            return None
        allowed = {"breakfast", "lunch", "snack", "dinner", "late_night"}
        if v not in allowed:
            raise ValueError(f"meal_type must be one of: {sorted(allowed)}")
        return v


class FoodLogsResponse(BaseModel):
    logs: list[FoodLogRead]
    total: int


# ── Voice Notes ────────────────────────────────────────────────────────────────

class VoiceNoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    child_id: str
    logged_at: datetime
    client_local_hour: int | None
    local_time_label: str | None
    raw_text: str
    sentences: list[str] = Field(default_factory=list)
    preliminary_category: list[str] = Field(default_factory=list)
    user_edited_text: str | None
    user_edited_at: datetime | None
    voided: bool
    voided_at: datetime | None


class VoiceNotesResponse(BaseModel):
    notes: list[VoiceNoteRead]
    total: int


# ── Activity Abstractions ──────────────────────────────────────────────────────

class AbstractionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    child_id: str
    log_date: date
    generated_at: datetime
    source_ids: dict[str, Any] = Field(default_factory=dict)
    categories: dict[str, Any] = Field(default_factory=dict)
    llm_summary: str | None
    raw_sentences_kept: list[str] = Field(default_factory=list)
    user_corrections: dict[str, Any] = Field(default_factory=dict)
    version: int
    is_current: bool


class AbstractionGenerateRequest(BaseModel):
    child_id: str = "default"
    log_date: date


class AbstractionPatchRequest(BaseModel):
    """Dot-notation corrections, e.g. {"meltdowns.count": 2, "mood_arc.morning": "positive"}."""
    corrections: dict[str, Any]
