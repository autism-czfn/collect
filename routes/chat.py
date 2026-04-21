"""
routes/chat.py — Conversation feature endpoints.

POST /api/chat/stream   SSE pipeline: planner → retrieval → Claude stream
GET  /api/chat/history  Load prior conversation turns for a child_id
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Annotated, AsyncGenerator, AsyncIterator

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import db as _db
from chat_planner import plan_chat
from chat_retrieval import (
    build_context,
    build_log_summary,
    retrieve_crawl,
    retrieve_live,
    retrieve_logs,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

_CLAUDE_CHAT_TIMEOUT    = 120   # seconds for the main answer stream
_CLAUDE_SUMMARY_TIMEOUT = 30    # seconds for background summarisation


# ── SSE helpers ───────────────────────────────────────────────────────────────

import uuid as _uuid
import datetime as _datetime

class _SafeEncoder(json.JSONEncoder):
    """Handle UUID, datetime, and other non-serializable types from asyncpg."""
    def default(self, obj):
        if isinstance(obj, _uuid.UUID):
            return str(obj)
        if isinstance(obj, (_datetime.datetime, _datetime.date)):
            return obj.isoformat()
        return super().default(obj)

def _sse(event: str, data: dict | str) -> str:
    payload = json.dumps(data, cls=_SafeEncoder) if isinstance(data, dict) else data
    return f"event: {event}\ndata: {payload}\n\n"


# ── Request / response models ─────────────────────────────────────────────────

class ChatRequest(BaseModel):
    child_id: str
    message: str
    language: str = "en"
    audience: str = "parent"   # "parent" | "clinician"


class MessageRead(BaseModel):
    role: str
    content: str
    created_at: str


class HistoryResponse(BaseModel):
    child_id: str
    messages: list[MessageRead]


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _get_recent_messages(
    child_id: str,
    pool,
    limit: int = 8,
) -> list[dict]:
    """Return recent chat turns (user + assistant + summary) ordered oldest-first."""
    rows = await pool.fetch(
        """
        SELECT role, content, created_at
        FROM   mzhu_test_chat_messages
        WHERE  child_id = $1
        ORDER BY created_at DESC
        LIMIT  $2
        """,
        child_id,
        limit,
    )
    return [
        {"role": r["role"], "content": r["content"]}
        for r in reversed(rows)   # oldest first for LLM context
    ]


async def _persist_messages(
    child_id: str,
    user_message: str,
    assistant_response: str,
    pool,
) -> int:
    """Append user + assistant turns. Returns new total message count for child."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO mzhu_test_chat_messages (child_id, role, content)
            VALUES ($1, 'user', $2), ($1, 'assistant', $3)
            """,
            child_id,
            user_message,
            assistant_response,
        )
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM mzhu_test_chat_messages WHERE child_id = $1",
            child_id,
        )
    return int(row["n"])


async def _persist_summary(child_id: str, summary: str, pool) -> None:
    """Store a condensed summary as role='summary' in place of older turns."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO mzhu_test_chat_messages (child_id, role, content)
            VALUES ($1, 'summary', $2)
            """,
            child_id,
            summary,
        )


# ── Phase 3: Background summarisation ─────────────────────────────────────────

async def _summarise_chat(child_id: str, pool) -> None:
    """Condense the last 10 turns into a summary stored as role='summary'.

    Fires as an asyncio background task every 5 turns. Never raises — failure
    is logged and silently dropped so it cannot affect the SSE stream.
    """
    try:
        rows = await pool.fetch(
            """
            SELECT role, content
            FROM   mzhu_test_chat_messages
            WHERE  child_id = $1
              AND  role IN ('user', 'assistant')
            ORDER BY created_at DESC
            LIMIT  10
            """,
            child_id,
        )
        if not rows:
            return

        turns = "\n".join(
            f"{r['role'].upper()}: {r['content'][:400]}" for r in reversed(rows)
        )
        prompt = (
            "Summarise this conversation in 3-5 sentences, preserving key facts "
            "about the child's behavior patterns, concerns raised, and any "
            "advice given. Be concise.\n\n" + turns
        )

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

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
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_CLAUDE_SUMMARY_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            log.warning("chat: background summarisation timed out after %ds", _CLAUDE_SUMMARY_TIMEOUT)
            return

        if proc.returncode != 0:
            log.warning(
                "chat: summarisation claude -p exit=%d stderr=%r",
                proc.returncode,
                stderr.decode(errors="replace").strip()[:200],
            )
            return

        summary_text = stdout.decode(errors="replace").strip()
        if not summary_text:
            log.warning("chat: summarisation returned empty output for child_id=%s", child_id)
            return

        await _persist_summary(child_id, summary_text, pool)
        log.info("chat: background summary stored for child_id=%s", child_id)

    except Exception as exc:
        log.warning("chat: background summarisation failed — %s", exc)


# ── Claude prompt builder ─────────────────────────────────────────────────────

def _build_system_prompt(context: dict, audience: str, language: str) -> str:
    child_ctx = context["child_context"]
    evidence = context["evidence"]
    conversation = context["conversation"]

    # Audience tone
    if audience == "clinician":
        tone = "Use clinical terminology. DSM/ICD framing is appropriate."
    else:
        tone = "Use plain language. Avoid clinical jargon."

    # Format logs (PRIMARY SIGNAL)
    log_lines = "\n".join(
        f"- [{l['logged_at'][:10]}] triggers={l['triggers']} "
        f"severity={l['severity']} time={l['time_of_day']}"
        for l in evidence["logs"][:10]
    ) or "No log events available."

    # Format crawl evidence with source for citation
    crawl_lines = "\n".join(
        f"- [{r.get('source_name', 'unknown')}] {r.get('title', '')}: "
        f"{str(r.get('snippet', ''))[:200]}"
        for r in evidence["crawled"][:5]
    ) or "No crawl evidence available."

    # Format live results with source for citation
    live_lines = "\n".join(
        f"- [{r.get('source_name', r.get('source', 'unknown'))}] "
        f"{r.get('title', '')}: {str(r.get('snippet', ''))[:200]}"
        for r in evidence["live"][:3]
    ) or "No live results available."

    # Format prior conversation
    history = "\n".join(
        f"{m['role'].upper()}: {m['content'][:300]}" for m in conversation[-4:]
    ) or "(start of conversation)"

    return f"""You are a compassionate expert assistant for families and clinicians supporting children with autism.

AUDIENCE: {audience}. {tone}
LANGUAGE: Respond in {language}.

CHILD'S LOG DATA (PRIMARY SIGNAL — always reference these first):
Trigger summary (30 days): {child_ctx['trigger_counts']}
Average severity: {child_ctx['avg_severity']}
Peak time of day: {child_ctx['peak_time_of_day']}
Co-occurrences: {child_ctx['co_occurrences']}

Recent log events:
{log_lines}

RESEARCH EVIDENCE (crawl DB):
{crawl_lines}

LIVE SEARCH RESULTS:
{live_lines}

PRIOR CONVERSATION:
{history}

RULES:
1. You MUST connect the child's log data to your response — reference specific triggers, times, or patterns.
2. You MUST cite sources when drawing on research evidence (use the source name in brackets, e.g. [CDC]).
3. Structure your response:
   1. What the child's logs show
   2. What research says
   3. Explanation
   4. Suggested actions
   5. Uncertainty / confidence level
4. Never diagnose. Flag safety concerns immediately."""


# ── Claude streaming via claude -p --output-format stream-json ───────────────

async def _claude_chat_stream(
    system_prompt: str,
    message: str,
) -> AsyncGenerator[str, None]:
    """Stream Claude's answer via `claude --output-format stream-json`.

    Yields text deltas as they arrive. Raises RuntimeError on timeout or failure.
    The stream-json format emits assistant events with cumulative text; we track
    the last seen length and yield only the new delta each time.
    """
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "claude",
        "--disable-slash-commands",
        "--tools", "",
        "--output-format", "stream-json",
        "--verbose",
        "--system-prompt", system_prompt,
        "-p", message,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    log.info("claude_chat LAUNCH pid=%s timeout=%ds", proc.pid, _CLAUDE_CHAT_TIMEOUT)

    last_text_len = 0
    full_text = ""
    deadline = time.monotonic() + _CLAUDE_CHAT_TIMEOUT

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(f"claude chat stream timed out after {_CLAUDE_CHAT_TIMEOUT}s")

        try:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(f"claude chat stream timed out after {_CLAUDE_CHAT_TIMEOUT}s")

        if not raw:
            break  # EOF — subprocess exited

        line = raw.decode(errors="replace").strip()
        if not line:
            continue

        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue   # non-JSON line (e.g. progress output); skip

        obj_type = obj.get("type")

        if obj_type == "assistant":
            # Each assistant event carries cumulative text; yield only the new delta
            content = obj.get("message", {}).get("content", [])
            for block in content:
                if block.get("type") == "text":
                    current_text = block.get("text", "")
                    delta = current_text[last_text_len:]
                    if delta:
                        last_text_len = len(current_text)
                        full_text = current_text
                        yield delta

        elif obj_type == "result":
            if obj.get("subtype") == "error":
                err = obj.get("error", "Claude error")
                log.error("claude_chat FAIL result error=%r", err)
                raise RuntimeError(err)
            # Final result event — yield any remaining text not yet streamed
            final = obj.get("result", "").strip()
            delta = final[last_text_len:]
            if delta:
                full_text = final
                yield delta

    # Drain stderr and reap process
    _, stderr_bytes = await proc.communicate()
    elapsed = int((time.monotonic() - t0) * 1000)

    if proc.returncode != 0:
        err = stderr_bytes.decode(errors="replace").strip()[:300]
        log.error("claude_chat FAIL exit=%d elapsed=%dms stderr=%r", proc.returncode, elapsed, err)
        raise RuntimeError(f"claude -p exited {proc.returncode}: {err}")

    log.info("claude_chat OK chars=%d elapsed=%dms", len(full_text), elapsed)


# ── SSE pipeline generator ────────────────────────────────────────────────────

async def _stream_pipeline(req: ChatRequest) -> AsyncIterator[str]:
    pool = _db.get_pool()
    child_id = req.child_id
    message = req.message
    language = req.language
    audience = req.audience

    full_response = ""

    try:
        # ── [1] Load context ──────────────────────────────────────────────────
        recent_messages = await _get_recent_messages(child_id, pool)

        # ── [2] Single LLM call ───────────────────────────────────────────────
        log_summary_prefetch = await _db.get_pool().fetchrow(
            """
            SELECT COUNT(*)                          AS total_events,
                   AVG(severity)                     AS avg_severity
            FROM   mzhu_test_logs
            WHERE  child_id = $1
              AND  logged_at > now() - INTERVAL '30 days'
              AND  NOT voided
            """,
            child_id,
        )
        # Lightweight prefetch for planner (full retrieval done in Stage 1)
        planner_summary = {
            "total_events": int(log_summary_prefetch["total_events"] or 0),
            "avg_severity": (
                round(float(log_summary_prefetch["avg_severity"]), 2)
                if log_summary_prefetch["avg_severity"]
                else None
            ),
            "trigger_counts": {},
            "peak_time_of_day": "unknown",
        }

        plan = await plan_chat(
            message, recent_messages, planner_summary, language, audience
        )
        intent         = plan["intent"]
        mode           = plan["mode"]
        rewritten_query = plan["rewritten_query"]
        sub_queries    = plan["sub_queries"]

        # ── [3] Stage 1: Log retrieval (sequential, before metadata emit) ────
        log_result = await retrieve_logs(child_id, pool)

        # Emit metadata (triggers now available from log retrieval)
        yield _sse("metadata", {
            "intent":          intent,
            "mode":            mode,
            "rewritten_query": rewritten_query,
            "sub_queries":     sub_queries,
            "triggers":        log_result["top_triggers"],
        })
        yield _sse("plan", {"subqueries": sub_queries})

        # ── Filter logs now — all data available (message + planner + log_result) ──
        # Priority 1: triggers verbatim in user's raw message (most specific).
        # Priority 2: triggers in planner-expanded query (rewritten + sub_queries).
        # Priority 3: top-3 trigger logs (default personal context).
        # Priority 4: all logs (last resort).
        def _norm(s: str) -> str:
            return s.lower().replace("_", " ").replace("-", " ")

        raw_text      = _norm(message)
        expanded_text = _norm(" ".join([message, rewritten_query] + sub_queries))
        all_logs      = log_result["logs"]
        top_triggers  = log_result["top_triggers"]
        top_norm      = {_norm(t) for t in top_triggers}

        def _matches_text(log_entry: dict, text: str) -> bool:
            return any(_norm(t) in text for t in (log_entry.get("triggers") or []))

        def _in_top_triggers(log_entry: dict) -> bool:
            return any(_norm(t) in top_norm for t in (log_entry.get("triggers") or []))

        relevant_logs = [l for l in all_logs if _matches_text(l, raw_text)]
        filter_reason = "raw_message"
        if not relevant_logs:
            relevant_logs = [l for l in all_logs if _matches_text(l, expanded_text)]
            filter_reason = "expanded_query"
        if not relevant_logs:
            relevant_logs = [l for l in all_logs if _in_top_triggers(l)]
            filter_reason = "top3_triggers"
        evidence_logs = relevant_logs[:20] if relevant_logs else all_logs[:20]
        if not relevant_logs:
            filter_reason = "all_logs_fallback"

        log.info(
            "chat: evidence logs total=%d relevant=%d filter=%s (query: %r)",
            len(all_logs), len(evidence_logs), filter_reason, rewritten_query[:60],
        )

        # ── Emit logs retrieval done + log evidence immediately ───────────────
        # Personal Logs panel populates as soon as Stage 1 finishes — before
        # Stage 2 (crawl + live) even starts.
        yield _sse("retrieval", {
            "source": "logs",
            "status": "ok",
            "count":  len(log_result["logs"]),
        })
        yield _sse("evidence_logs", {"logs": evidence_logs})

        # ── [4] Stage 2: Crawl + Live run in parallel as independent tasks ──────
        # Each emits its own SSE event the moment it finishes — no waiting for
        # the other.  Crawl (local DB) typically finishes in <1 s; live search
        # can take up to 15 s.  Both start simultaneously via create_task().
        crawl_task = asyncio.create_task(retrieve_crawl(rewritten_query, sub_queries))
        live_task  = asyncio.create_task(retrieve_live(rewritten_query, audience))

        # Await crawl first — it's fast; live_task is already running in background.
        crawl_result = await crawl_task
        yield _sse("retrieval", {
            "source":   "crawl",
            "status":   "ok",
            "count":    len(crawl_result["results"]),
            "degraded": crawl_result["degraded"],
        })
        yield _sse("evidence_crawl", {"crawled": crawl_result["results"][:10]})

        # Now await live — may still be in flight; emit as soon as it lands.
        live_result     = await live_task
        live_results    = live_result.get("results", [])
        sites_attempted = live_result.get("sites_attempted", 0)
        search_query    = live_result.get("search_query")
        yield _sse("retrieval", {
            "source":          "live",
            "status":          "ok",
            "count":           len(live_results),
            "sites_attempted": sites_attempted,
        })
        yield _sse("evidence_live", {
            "live":           live_results[:5],
            "sites_attempted": sites_attempted,
            "search_query":   search_query,
        })

        # ── [5] Context builder ───────────────────────────────────────────────
        context = build_context(
            log_result, crawl_result, live_result,
            recent_messages, language, audience,
        )

        # ── [6] Claude streaming via claude -p --output-format stream-json ──────
        system_prompt = _build_system_prompt(context, audience, language)
        async for chunk in _claude_chat_stream(system_prompt, message):
            full_response += chunk
            yield _sse("answer_chunk", {"text": chunk})

        # ── [6] Persist messages ──────────────────────────────────────────────
        msg_count = await _persist_messages(child_id, message, full_response, pool)

        # ── Phase 3: Background summarisation (every 5 turns) ────────────────
        # msg_count counts all roles; /2 approximates user+assistant pairs
        if (msg_count // 2) % 5 == 0 and msg_count > 0:
            asyncio.create_task(_summarise_chat(child_id, pool))

        yield _sse("done", {})

    except Exception as exc:
        log.error("chat_stream pipeline error — %s", exc, exc_info=True)
        yield _sse("error", {"message": "Internal error — please try again"})


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/api/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """SSE stream: planner → log retrieval → parallel retrieval → Claude stream."""
    return StreamingResponse(
        _stream_pipeline(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",     # disable nginx buffering
        },
    )


@router.get("/api/chat/history", response_model=HistoryResponse)
async def chat_history(
    child_id: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> HistoryResponse:
    """Return prior conversation turns (user + assistant only) ordered oldest-first.

    Used by the UI on page load to seed the conversation from DB.
    Returns messages: [] (never null) if no history exists.
    """
    pool = _db.get_pool()
    rows = await pool.fetch(
        """
        SELECT role, content, created_at
        FROM   mzhu_test_chat_messages
        WHERE  child_id = $1
          AND  role IN ('user', 'assistant')
        ORDER BY created_at ASC
        LIMIT  $2
        """,
        child_id,
        limit,
    )
    return HistoryResponse(
        child_id=child_id,
        messages=[
            MessageRead(
                role=r["role"],
                content=r["content"],
                created_at=r["created_at"].isoformat(),
            )
            for r in rows
        ],
    )
