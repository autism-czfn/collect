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
