from __future__ import annotations
"""
Safety webhook — P-COL-3.

Fires POST /api/safety-webhook to the search service when a safety-critical
log is created. Runs in background — never blocks log creation.

Canonical target: https://<search-host>:3001/api/safety-webhook
Retry policy: 3 attempts with exponential backoff [1s, 2s, 4s].
Failure logging: log.error per attempt, log.critical after exhaustion.

Config:
  SEARCH_WEBHOOK_URL env var — e.g. "https://localhost:3001/api/safety-webhook"
  If not set, webhooks are disabled (warning logged at startup via main.py).
"""

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

SEARCH_WEBHOOK_URL = os.getenv("SEARCH_WEBHOOK_URL")

WEBHOOK_TIMEOUT = 5.0          # seconds per attempt
_RETRY_DELAYS   = [1, 2, 4]   # backoff between attempts (total ~7s max)


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

# Canonical trigger_type enum (matches SAFETY WEBHOOK CONTRACT in plan §3)
_CONTRACT_ENUM = frozenset({
    "self_harm", "violence", "abuse",
    "elopement", "aggression", "emergency",
})

# Normalise keys that are detected but not in the CONTRACT enum as distinct values
_TRIGGER_NORMALIZE: dict[str, str] = {
    "suicide": "self_harm",   # suicide is self_harm clinically
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


def _determine_webhook_trigger(
    text: str,
    triggers: list[str],
    severity: int | None,
) -> str | None:
    """Return the trigger_type for the webhook payload, or None if no webhook needed.

    Priority:
      1. Intent-based detection on full text (catches semantic signals)
      2. severity >= 4 with any known trigger (high-severity behavioural event)

    Normalises "suicide" → "self_harm" per CONTRACT enum rules.
    """
    safety_level, safety_key = _detect_safety(text)

    if safety_key:
        trigger_type = _TRIGGER_NORMALIZE.get(safety_key, safety_key)
        # Only fire if trigger_type is in the CONTRACT enum; skip non-contract
        # signals like "medication_concern" which the search service doesn't handle.
        return trigger_type if trigger_type in _CONTRACT_ENUM else None

    if severity is not None and severity >= 4 and triggers:
        candidate = triggers[0]
        trigger_type = _TRIGGER_NORMALIZE.get(candidate, candidate)
        return trigger_type if trigger_type in _CONTRACT_ENUM else None

    return None


# ── Webhook send with retry ──────────────────────────────────────────────────

async def _send_webhook(payload: dict) -> None:
    """Send webhook with 3× retry + exponential backoff. Never raises.

    Logs:
      - log.error  on each failed attempt (status, full payload)
      - log.critical after all 3 retries exhausted
    """
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(verify=False, timeout=WEBHOOK_TIMEOUT) as client:
                resp = await client.post(SEARCH_WEBHOOK_URL, json=payload)
            if 200 <= resp.status_code < 300:
                log.info(
                    "Webhook sent: trigger=%s child=%s status=%d",
                    payload.get("trigger_type"),
                    payload.get("child_id"),
                    resp.status_code,
                )
                return
            log.error(
                "Webhook attempt %d/3 failed: status=%d trigger=%s child=%s payload=%s",
                attempt, resp.status_code,
                payload.get("trigger_type"), payload.get("child_id"), payload,
            )
        except httpx.TimeoutException:
            log.error(
                "Webhook attempt %d/3 timed out (%.1fs): trigger=%s child=%s",
                attempt, WEBHOOK_TIMEOUT,
                payload.get("trigger_type"), payload.get("child_id"),
            )
        except Exception as e:
            log.error(
                "Webhook attempt %d/3 error: %s — trigger=%s child=%s payload=%s",
                attempt, e,
                payload.get("trigger_type"), payload.get("child_id"), payload,
            )

        if attempt < 3:
            await asyncio.sleep(_RETRY_DELAYS[attempt - 1])

    log.critical(
        "Webhook EXHAUSTED after 3 attempts — safety signal may be lost. "
        "trigger=%s child=%s payload=%s",
        payload.get("trigger_type"), payload.get("child_id"), payload,
    )


# ── Public API ───────────────────────────────────────────────────────────────

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

    Payload conforms to SAFETY WEBHOOK CONTRACT (plan §3):
      event_id, child_id, trigger_type, severity, raw_text,
      normalized_intent, timestamp, source: "collect"

    Args:
        child_id:   child identifier
        triggers:   normalized trigger list from the log
        severity:   severity rating (1–5 or None)
        tags:       log tags (unused in payload, kept for call-site compat)
        logged_at:  when the log was created
        event_text: raw caregiver text — used for intent detection + raw_text field
    """
    if not SEARCH_WEBHOOK_URL:
        return

    full_text = " ".join([event_text] + triggers)
    trigger_type = _determine_webhook_trigger(full_text, triggers, severity)
    if trigger_type is None:
        return

    payload = {
        "event_id":          str(uuid.uuid4()),
        "child_id":          child_id,
        "trigger_type":      trigger_type,
        "severity":          severity,
        "raw_text":          event_text,
        "normalized_intent": trigger_type,
        "timestamp":         (logged_at or datetime.now(timezone.utc)).isoformat(),
        "source":            "collect",
    }

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_send_webhook(payload))
        log.info(
            "Webhook scheduled: trigger=%s child=%s severity=%s",
            trigger_type, child_id, severity,
        )
    except RuntimeError:
        log.warning("No event loop — webhook could not be scheduled for trigger=%s", trigger_type)
