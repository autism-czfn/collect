"""
GET /logs/trigger-signals — enriched trigger signals for search repo.

Groups logs by trigger over a rolling window, computing frequency,
severity, time-of-day distribution, and common contexts/environments.

Contract consumer: search repo Priority 5 (trigger_policy.py).
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from db import get_pool

router = APIRouter(tags=["trigger-signals"])


# ── Time-of-day derivation ────────────────────────────────────────────────────
# Boundaries (UTC hour):
#   morning:   06:00-11:59
#   afternoon: 12:00-16:59
#   evening:   17:00-20:59
#   night:     21:00-05:59

def _time_of_day(hour: int) -> str:
    if 6 <= hour <= 11:
        return "morning"
    if 12 <= hour <= 16:
        return "afternoon"
    if 17 <= hour <= 20:
        return "evening"
    return "night"


# ── Environment derivation ────────────────────────────────────────────────────
# Priority order: school > public_place > home
# Non-location tags (sensory, evening, morning, after-therapy) are ignored.

_LOCATION_PRIORITY = ["school", "public_place", "home"]


def _environment_from_tags(tags: list[str]) -> str | None:
    for loc in _LOCATION_PRIORITY:
        if loc in tags:
            return loc
    return None


# ── Response models ───────────────────────────────────────────────────────────

class TimeOfDayDistribution(BaseModel):
    morning: int = 0
    afternoon: int = 0
    evening: int = 0
    night: int = 0


class TriggerSignal(BaseModel):
    trigger: str
    count: int
    first_seen: datetime
    last_seen: datetime
    avg_severity: float | None
    common_contexts: list[str]
    common_environments: list[str]
    time_of_day_distribution: TimeOfDayDistribution


class TriggerSignalsResponse(BaseModel):
    child_id: str
    period_days: int
    trigger_signals: list[TriggerSignal]


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.get("/logs/trigger-signals", response_model=TriggerSignalsResponse)
async def get_trigger_signals(
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    child_id: str = "default",
):
    """Return enriched trigger signals grouped by trigger over the last N days."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                logged_at,
                triggers,
                tags,
                context,
                severity
            FROM mzhu_test_logs
            WHERE logged_at >= now() - $1 * interval '1 day'
              AND NOT voided
              AND ($2 = 'default' OR child_id = $2)
            ORDER BY logged_at DESC
            """,
            days,
            child_id,
        )

    # Aggregate in Python — small result sets, complex grouping logic
    from collections import defaultdict

    trigger_data: dict[str, dict] = defaultdict(lambda: {
        "count": 0,
        "first_seen": None,
        "last_seen": None,
        "severities": [],
        "contexts": defaultdict(int),
        "environments": defaultdict(int),
        "tod": {"morning": 0, "afternoon": 0, "evening": 0, "night": 0},
    })

    for row in rows:
        triggers = row["triggers"] or []
        logged_at: datetime = row["logged_at"]
        tags = row["tags"] or []
        context = row["context"]
        severity = row["severity"]

        tod = _time_of_day(logged_at.hour)
        env = _environment_from_tags(tags)

        for t in triggers:
            d = trigger_data[t]
            d["count"] += 1

            if d["first_seen"] is None or logged_at < d["first_seen"]:
                d["first_seen"] = logged_at
            if d["last_seen"] is None or logged_at > d["last_seen"]:
                d["last_seen"] = logged_at

            if severity is not None:
                d["severities"].append(severity)
            if context:
                # Take first 80 chars as a context key to avoid unbounded cardinality
                ctx_key = context[:80].strip()
                d["contexts"][ctx_key] += 1
            if env:
                d["environments"][env] += 1
            d["tod"][tod] += 1

    # Build response
    signals = []
    for trigger, d in sorted(trigger_data.items(), key=lambda x: -x[1]["count"]):
        avg_sev = (
            round(sum(d["severities"]) / len(d["severities"]), 2)
            if d["severities"]
            else None
        )
        # Top 5 contexts by frequency
        top_contexts = [
            k for k, _ in sorted(d["contexts"].items(), key=lambda x: -x[1])[:5]
        ]
        # All environments (there are only 3 possible)
        top_envs = [
            k for k, _ in sorted(d["environments"].items(), key=lambda x: -x[1])
        ]
        signals.append(TriggerSignal(
            trigger=trigger,
            count=d["count"],
            first_seen=d["first_seen"],
            last_seen=d["last_seen"],
            avg_severity=avg_sev,
            common_contexts=top_contexts,
            common_environments=top_envs,
            time_of_day_distribution=TimeOfDayDistribution(**d["tod"]),
        ))

    return TriggerSignalsResponse(
        child_id=child_id,
        period_days=days,
        trigger_signals=signals,
    )
