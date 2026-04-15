from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


OutcomeType = Literal["calm", "mild_distress", "meltdown", "regression", "positive"]


# ── Logs ──────────────────────────────────────────────────────────────────────

class LogCreate(BaseModel):
    event: str = Field(..., max_length=2000)
    triggers: list[str] = Field(default_factory=list)
    context: str | None = Field(None, max_length=5000)
    response: str | None = Field(None, max_length=5000)
    outcome: OutcomeType
    intervention_ids: list[uuid.UUID] = Field(default_factory=list)

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


class LogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    logged_at: datetime
    event: str
    triggers: list[str]
    context: str | None
    response: str | None
    outcome: str
    intervention_ids: list[uuid.UUID]
    voided: bool
    voided_at: datetime | None


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

    @field_validator("week_start")
    @classmethod
    def must_be_monday(cls, v: date) -> date:
        if v.weekday() != 0:
            raise ValueError(
                f"week_start must be a Monday; {v} is a {v.strftime('%A')}"
            )
        return v


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
    ratings: dict[str, int]
    notes: str | None = None

    @field_validator("check_date")  # mode="after" (default) — value is a date object
    @classmethod
    def not_in_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("check_date cannot be in the future")
        return v

    @field_validator("ratings", mode="before")  # raw dict before Pydantic coercion
    @classmethod
    def validate_ratings(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            raise ValueError("ratings must be an object")
        missing = VALID_RATING_KEYS - v.keys()
        if missing:
            raise ValueError(f"ratings missing required keys: {sorted(missing)}")
        extra = v.keys() - VALID_RATING_KEYS
        if extra:
            raise ValueError(f"ratings contains unknown keys: {sorted(extra)}")
        for key in RATING_1_5_KEYS:
            val = v[key]
            if not isinstance(val, int) or not (1 <= val <= 5):
                raise ValueError(f"ratings.{key} must be an integer between 1 and 5")
        mc = v["meltdown_count"]
        if not isinstance(mc, int) or mc < 0:
            raise ValueError("ratings.meltdown_count must be an integer >= 0")
        return v

    @field_validator("notes")  # mode="after" (default)
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
