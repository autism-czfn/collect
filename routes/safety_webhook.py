from __future__ import annotations
"""
Safety webhook (Collect P3.1).

Fire-and-forget notification to the search service when a safety-critical
log is created. Runs in background — never blocks log creation.

Uses intent-based safety detection (semantic regex patterns) instead of
keyword matching, so "hurt himself", "end his life", "suicide" are all
caught without needing exact trigger vocabulary entries.

Config:
  SEARCH_WEBHOOK_URL env var (e.g. "https://localhost:18000/api/webhooks/trigger-event")
  If not set, webhook is silently disabled.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

SEARCH_WEBHOOK_URL = os.getenv("SEARCH_WEBHOOK_URL")

# Timeout for webhook call — fire-and-forget, don't block
WEBHOOK_TIMEOUT = 2.0


# ── Intent-based safety detection ───────────────────────────────────────────
# Mirrors the search repo's intent_classifier.py patterns.
# Kept as a standalone copy to avoid cross-repo import dependency.

_SAFETY_HIGH_PATTERNS: dict[str, re.Pattern] = {
    "suicide": re.compile(
        r"\bsuicid\w*\b|"
        r"\bkill\s+(him|her|my|them)sel[fv]\w*\b|"
        r"\bend\s+(his|her|my|their)\s+life\b|"
        r"\bwant\w*\s+to\s+die\b|"
        r"\bdon'?t\s+want\s+to\s+(live|be\s+alive)\b|"
        r"自杀|想死|不想活|轻生|結束生命|了结",
        re.IGNORECASE,
    ),
    "self_harm": re.compile(
        r"\bself[- ]?harm\w*\b|"
        r"\bself[- ]?injur\w*\b|"
        r"\bhurt\w*\s+(him|her|my|them)sel[fv]\w*\b|"
        r"\bbang\w*\s+(his|her|my|their)\s+head\b|"
        r"\bhead[- ]?bang\w*\b|"
        r"\bcutting\s+(him|her|my|them)sel[fv]\w*\b|"
        r"\bbiting\s+(him|her|my|them)sel[fv]\w*\b|"
        r"自伤|自傷|自残|自殘|撞头|撞頭|咬自己",
        re.IGNORECASE,
    ),
    "abuse": re.compile(
        r"\babuse[ds]?\b|\bmolest\w*\b|\bneglect\w*\b|"
        r"虐待|忽视|忽視|性侵",
        re.IGNORECASE,
    ),
    "violence": re.compile(
        r"\bviolence\b|\bviolent\s+behav\w*\b|"
        r"\battack\w*\s+(other|people|kids|children|parent|teacher)\b|"
        r"\bthreat\w*\s+to\s+(kill|harm|hurt)\b|"
        r"暴力|打人|攻击|攻擊",
        re.IGNORECASE,
    ),
    "emergency": re.compile(
        r"\bemergency\b|\bcrisis\b|\bdanger\w*\b|"
        r"\bseizure\w*\b|\boverdos\w*\b|\bpoisoning\b|\bunconscious\b|"
        r"紧急|緊急|危险|危險|昏迷|癫痫|癲癇|中毒",
        re.IGNORECASE,
    ),
    "elopement": re.compile(
        r"\belopement\b|\bran\s+away\b|\brunning\s+away\b|"
        r"\bwander\w*\s+(off|away)\b|\bmissing\s+child\b|\bbolting\b|"
        r"走失|跑掉|走丢|走丟|跑出去",
        re.IGNORECASE,
    ),
}

_SAFETY_MEDIUM_PATTERNS: dict[str, re.Pattern] = {
    "aggression": re.compile(
        r"\baggress\w*\b|"
        r"\bhit\w*\s+(me|parent|sibling|brother|sister|teacher|other)\b|"
        r"\bkick\w*\s+(me|parent|sibling|brother|sister|teacher|other)\b|"
        r"\bbit\w*\s+(me|parent|sibling|brother|sister|teacher|other)\b|"
        r"\bdestruct\w*\s+behav\w*\b|\bthrowing\s+things\b|"
        r"打架|踢人|咬人|摔东西|摔東西",
        re.IGNORECASE,
    ),
    "medication_concern": re.compile(
        r"\bside\s+effect\w*\b|\badverse\s+(reaction|effect)\b|"
        r"副作用|不良反应|不良反應",
        re.IGNORECASE,
    ),
}


def _detect_safety(text: str) -> tuple[str | None, str | None]:
    """Detect safety level from free text using semantic patterns.

    Returns:
        (safety_level, matched_key) — e.g. ("HIGH", "suicide") or (None, None)
    """
    for key, pattern in _SAFETY_HIGH_PATTERNS.items():
        if pattern.search(text):
            return "HIGH", key
    for key, pattern in _SAFETY_MEDIUM_PATTERNS.items():
        if pattern.search(text):
            return "MEDIUM", key
    return None, None


# ── Webhook logic ───────────────────────────────────────────────────────────

def _determine_event_type(
    text: str,
    triggers: list[str],
    severity: int | None,
) -> tuple[str | None, str | None]:
    """Determine webhook event_type using intent detection on the full text.

    Returns:
        (event_type, safety_key) — e.g. ("safety_alert", "suicide") or (None, None)
    """
    # Intent-based detection on full event text (catches "suicide", "hurt himself", etc.)
    safety_level, safety_key = _detect_safety(text)

    if safety_level == "HIGH":
        return "safety_alert", safety_key
    if safety_level == "MEDIUM":
        return "safety_alert", safety_key
    if severity is not None and severity >= 4:
        return "high_severity", triggers[0] if triggers else "unknown"
    return None, None


async def _send_webhook(payload: dict) -> None:
    """Send webhook payload. Logs on failure, never raises."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=WEBHOOK_TIMEOUT) as client:
            resp = await client.post(SEARCH_WEBHOOK_URL, json=payload)
            log.info(
                "Webhook sent: event=%s trigger=%s status=%d",
                payload.get("event_type"),
                payload.get("trigger"),
                resp.status_code,
            )
    except httpx.TimeoutException:
        log.warning("Webhook timeout (%.1fs) for event=%s", WEBHOOK_TIMEOUT, payload.get("event_type"))
    except Exception as e:
        log.warning("Webhook failed: %s (event=%s)", e, payload.get("event_type"))


def fire_safety_webhook(
    child_id: str,
    triggers: list[str],
    severity: int | None,
    tags: list[str],
    logged_at: datetime | None = None,
    event_text: str = "",
) -> None:
    """Check if webhook should fire and schedule it in background.

    Called from create_log after DB write. Never blocks, never raises.

    Args:
        child_id: child identifier
        triggers: normalized trigger list from the log
        severity: severity rating (1-5 or None)
        tags: log tags
        logged_at: when the log was created
        event_text: raw event description — used for intent-based safety detection
    """
    if not SEARCH_WEBHOOK_URL:
        return

    # Build full text for intent detection: event + triggers + notes
    full_text = " ".join([event_text] + triggers)

    event_type, safety_key = _determine_event_type(full_text, triggers, severity)
    if event_type is None:
        return

    payload = {
        "event_type": event_type,
        "child_id": child_id,
        "trigger": safety_key or (triggers[0] if triggers else "unknown"),
        "severity": severity,
        "tags": tags,
        "logged_at": (logged_at or datetime.now(timezone.utc)).isoformat(),
    }

    # Fire and forget — don't await, don't block log creation
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_send_webhook(payload))
        log.info("Webhook scheduled: event=%s trigger=%s", event_type, safety_key)
    except RuntimeError:
        log.warning("No event loop — skipping webhook")
