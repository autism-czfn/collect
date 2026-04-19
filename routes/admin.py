from __future__ import annotations
"""
Admin endpoints — P-COL-2: trigger vocabulary management.

GET  /admin/unknown-triggers            — list candidates ranked by frequency
POST /admin/unknown-triggers/{text}/promote — promote to canonical or alias

These endpoints require no auth in the current single-user deployment.
Add auth middleware before exposing externally.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from db import get_pool
import trigger_vocab

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

_TRIGGERS_PATH = Path(__file__).parent.parent / "config" / "triggers.json"


# ── Response models ───────────────────────────────────────────────────────────

class UnknownTrigger(BaseModel):
    trigger_text: str
    count: int
    first_seen: str
    last_seen: str


class UnknownTriggersResponse(BaseModel):
    unknown_triggers: list[UnknownTrigger]
    total: int


class PromoteResponse(BaseModel):
    trigger_text: str
    action: str          # "canonical" or "alias"
    alias_for: str | None
    canonical_triggers: list[str]
    message: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_triggers_json() -> dict:
    with open(_TRIGGERS_PATH) as f:
        return json.load(f)


def _write_triggers_json(config: dict) -> None:
    """Write atomically via temp file + rename to avoid partial writes."""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=_TRIGGERS_PATH.parent,
            suffix=".tmp",
            delete=False,
        ) as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.write("\n")
            tmp = f.name
        os.replace(tmp, _TRIGGERS_PATH)
    except Exception:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/unknown-triggers", response_model=UnknownTriggersResponse)
async def list_unknown_triggers(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    min_count: Annotated[int, Query(ge=1)] = 1,
):
    """List unknown triggers submitted by caregivers, ranked by frequency.

    Use min_count to filter noise (e.g. min_count=2 shows triggers seen
    at least twice — more likely to be genuinely useful candidates).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT trigger_text, count, first_seen, last_seen
            FROM mzhu_test_unknown_triggers
            WHERE count >= $1
            ORDER BY count DESC, last_seen DESC
            LIMIT $2
            """,
            min_count,
            limit,
        )

    items = [
        UnknownTrigger(
            trigger_text=r["trigger_text"],
            count=r["count"],
            first_seen=r["first_seen"].isoformat(),
            last_seen=r["last_seen"].isoformat(),
        )
        for r in rows
    ]
    return UnknownTriggersResponse(unknown_triggers=items, total=len(items))


@router.post("/unknown-triggers/{trigger_text}/promote", response_model=PromoteResponse)
async def promote_unknown_trigger(
    trigger_text: str,
    alias_for: Annotated[str | None, Query(
        description="If set, add trigger_text as an alias for this existing canonical "
                    "trigger instead of adding it as a new canonical entry."
    )] = None,
):
    """Promote an unknown trigger to the canonical vocabulary.

    Two modes:
      - No alias_for:  adds trigger_text as a new canonical trigger.
      - alias_for=X:   adds trigger_text as an alias mapping to existing canonical X.

    In both cases the trigger is removed from mzhu_test_unknown_triggers and
    the vocab module is reloaded so new logs use the updated vocabulary immediately.
    """
    config = _read_triggers_json()
    canonical = config.get("canonical_triggers", [])
    aliases   = config.get("aliases", {})

    normalized = trigger_text.strip().lower().replace("-", "_")

    if alias_for is not None:
        # ── alias mode ────────────────────────────────────────────────────────
        alias_for_norm = alias_for.strip().lower()
        if alias_for_norm not in canonical:
            raise HTTPException(
                status_code=422,
                detail=f"alias_for '{alias_for_norm}' is not a canonical trigger. "
                       f"Existing canonicals: {sorted(canonical)}",
            )
        if normalized in aliases:
            raise HTTPException(
                status_code=409,
                detail=f"'{normalized}' is already an alias for '{aliases[normalized]}'",
            )
        if normalized in canonical:
            raise HTTPException(
                status_code=409,
                detail=f"'{normalized}' is already a canonical trigger",
            )
        aliases[normalized] = alias_for_norm
        action = "alias"
        log.info("Promoting '%s' as alias for '%s'", normalized, alias_for_norm)

    else:
        # ── canonical mode ────────────────────────────────────────────────────
        if normalized in canonical:
            raise HTTPException(
                status_code=409,
                detail=f"'{normalized}' is already a canonical trigger",
            )
        if normalized in aliases:
            raise HTTPException(
                status_code=409,
                detail=f"'{normalized}' is already an alias for '{aliases[normalized]}'. "
                       "Use alias_for to update its target instead.",
            )
        canonical.append(normalized)
        canonical.sort()
        action = "canonical"
        log.info("Promoting '%s' as new canonical trigger", normalized)

    config["canonical_triggers"] = canonical
    config["aliases"] = aliases
    _write_triggers_json(config)

    # Reload vocab module so new logs use the updated vocabulary immediately
    trigger_vocab._load()
    log.info("Trigger vocab reloaded: %d canonical, %d aliases",
             len(trigger_vocab.CANONICAL_TRIGGERS), len(trigger_vocab.ALIASES))

    # Remove from unknown triggers table
    pool = get_pool()
    async with pool.acquire() as conn:
        deleted = await conn.execute(
            "DELETE FROM mzhu_test_unknown_triggers WHERE trigger_text = $1",
            normalized,
        )
    log.info("Removed '%s' from unknown triggers (%s)", normalized, deleted)

    return PromoteResponse(
        trigger_text=normalized,
        action=action,
        alias_for=alias_for,
        canonical_triggers=sorted(canonical),
        message=f"'{normalized}' promoted as {action}"
                + (f" → '{alias_for}'" if alias_for else "")
                + ". Vocabulary reloaded.",
    )
