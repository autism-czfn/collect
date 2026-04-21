"""
chat_planner.py — Single LLM call: intent classification + query rewrite.

Input:  raw message, recent chat history (list of {role, content}), log summary dict
Output: { intent, mode, rewritten_query, sub_queries }

IMPORTANT: `mode` is derived deterministically from `intent` by this module —
the LLM never produces `mode`. This avoids non-determinism in pipeline routing.

Raises ValueError on invalid LLM output; caller should return HTTP 500.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

log = logging.getLogger(__name__)

INTENT_VALUES = frozenset({"BEHAVIOR_PATTERN", "INTERVENTION", "MEDICAL", "SAFETY"})

# Keywords that suggest an intervention question (HYBRID_EVIDENCE_FIRST mode)
_INTERVENTION_KEYWORDS = frozenset({
    "how to", "what should", "help with", "strategy", "technique",
    "approach", "intervention", "handle", "manage", "deal with",
    "what can i", "what to do", "how do i", "how can i",
})

_PLANNER_TIMEOUT = 60   # seconds; planner call is short (512 tokens max)


def derive_mode(intent: str, query: str, has_logs: bool) -> str:
    """Deterministic mode selection — never produced by the LLM.

    IF intent == SAFETY                → SAFETY_EXPANDED_MODE
    ELIF 'why' in query AND logs exist → EXPLAIN_PATTERN
    ELIF intervention question          → HYBRID_EVIDENCE_FIRST
    ELSE                               → HYBRID_LOG_FIRST
    """
    if intent == "SAFETY":
        return "SAFETY_EXPANDED_MODE"
    q = query.lower()
    if "why" in q and has_logs:
        return "EXPLAIN_PATTERN"
    if any(kw in q for kw in _INTERVENTION_KEYWORDS):
        return "HYBRID_EVIDENCE_FIRST"
    return "HYBRID_LOG_FIRST"


async def _run_claude(prompt: str) -> str:
    """Run `claude -p <prompt>` and return stdout. Raises RuntimeError on failure."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)   # allow nested claude -p call

    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "claude",
        "--disable-slash-commands",
        "--tools", "",
        "--system-prompt", "",
        "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    log.info("chat_planner LAUNCH claude -p pid=%s timeout=%ds", proc.pid, _PLANNER_TIMEOUT)

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_PLANNER_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(f"chat_planner: claude -p timed out after {_PLANNER_TIMEOUT}s")

    elapsed = int((time.monotonic() - t0) * 1000)

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()[:300]
        log.error("chat_planner FAIL exit=%d elapsed=%dms stderr=%r", proc.returncode, elapsed, err)
        raise RuntimeError(f"claude -p exited {proc.returncode}: {err}")

    output = stdout.decode(errors="replace").strip()
    if not output:
        raise RuntimeError("chat_planner: claude -p returned empty output")

    log.info("chat_planner OK chars=%d elapsed=%dms", len(output), elapsed)
    return output


async def plan_chat(
    message: str,
    recent_messages: list[dict[str, str]],
    log_summary: dict[str, Any],
    language: str = "en",
    audience: str = "parent",
) -> dict[str, Any]:
    """Classify intent, rewrite query, generate sub-queries via a single LLM call.

    Returns:
        {
            "intent":          "BEHAVIOR_PATTERN | INTERVENTION | MEDICAL | SAFETY",
            "mode":            "<derived deterministically from intent>",
            "rewritten_query": "<clear search query>",
            "sub_queries":     ["<aspect 1>", "<aspect 2>"],
        }

    Raises:
        ValueError: if LLM returns unparseable JSON or invalid intent after one retry.
    """
    has_logs = log_summary.get("total_events", 0) > 0

    # Format recent conversation (last 6 turns to stay within prompt budget)
    history_text = "\n".join(
        f"{m['role'].upper()}: {m['content'][:300]}" for m in recent_messages[-6:]
    ) or "(no prior conversation)"

    # Format log summary
    trigger_list = ", ".join(
        list(log_summary.get("trigger_counts", {}).keys())[:5]
    ) or "none"
    log_text = (
        f"Total events (30 days): {log_summary.get('total_events', 0)}\n"
        f"Top triggers: {trigger_list}\n"
        f"Avg severity: {log_summary.get('avg_severity', 'n/a')}\n"
        f"Peak time of day: {log_summary.get('peak_time_of_day', 'unknown')}"
    ) if has_logs else "(no log data available for this child)"

    prompt = f"""You help parents and clinicians understand autism-related behavior.

Analyze the message below and return a JSON object with EXACTLY these three fields:

{{
  "intent": "<BEHAVIOR_PATTERN | INTERVENTION | MEDICAL | SAFETY>",
  "rewritten_query": "<clear, specific query capturing the core question>",
  "sub_queries": ["<specific aspect 1>", "<specific aspect 2>"]
}}

INTENT definitions:
- BEHAVIOR_PATTERN: trends, patterns, frequency, causes of behavior
- INTERVENTION:     strategies, techniques, how to respond or help
- MEDICAL:          diagnoses, medications, medical or clinical aspects
- SAFETY:           any safety concern — self-harm, elopement, crisis, danger

Recent conversation:
{history_text}

Child's log summary (last 30 days):
{log_text}

Current message: {message}

Return ONLY the JSON object. No explanation, no markdown."""

    raw = ""

    for attempt in range(2):
        try:
            raw = await _run_claude(prompt)

            # Strip markdown code fences if present
            if raw.startswith("```"):
                parts = raw.split("```")
                raw = parts[1] if len(parts) > 1 else raw
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            parsed = json.loads(raw)
            break

        except (json.JSONDecodeError, IndexError) as exc:
            if attempt == 0:
                log.warning("chat_planner: JSON parse failed (attempt 1), retrying — %s", exc)
                continue
            log.error(
                "chat_planner: JSON parse failed after retry — %s | raw=%r", exc, raw
            )
            raise ValueError(f"LLM returned invalid JSON: {exc}") from exc

        except RuntimeError as exc:
            if attempt == 0:
                log.warning("chat_planner: claude -p failed (attempt 1), retrying — %s", exc)
                continue
            raise ValueError(str(exc)) from exc

    # ── Validate intent ───────────────────────────────────────────────────────
    intent = str(parsed.get("intent", "")).strip()
    if intent not in INTENT_VALUES:
        log.error(
            "chat_planner: invalid intent %r — expected one of %s", intent, INTENT_VALUES
        )
        raise ValueError(f"Invalid intent from LLM: {intent!r}")

    # ── Validate rewritten_query (fallback to raw message) ───────────────────
    rewritten_query = str(parsed.get("rewritten_query", "")).strip()
    if not rewritten_query:
        log.warning("chat_planner: empty rewritten_query — falling back to raw message")
        rewritten_query = message

    # ── Sub-queries (best-effort) ─────────────────────────────────────────────
    sub_queries = parsed.get("sub_queries", [])
    if not isinstance(sub_queries, list):
        sub_queries = []
    sub_queries = [str(q).strip() for q in sub_queries if str(q).strip()]

    # ── Derive mode deterministically ─────────────────────────────────────────
    mode = derive_mode(intent, rewritten_query, has_logs)

    log.info(
        "chat_planner: intent=%s mode=%s query=%r sub_queries=%d",
        intent, mode, rewritten_query[:60], len(sub_queries),
    )

    return {
        "intent": intent,
        "mode": mode,
        "rewritten_query": rewritten_query,
        "sub_queries": sub_queries,
    }
