"""
Shared trigger vocabulary — loaded once from config/triggers.json.

Used by:
  - routes/logs.py          (POST /logs normalization)
  - routes/transcribe_and_log.py  (LLM output validation)
  - routes/triggers.py      (GET /triggers/vocabulary)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent / "config" / "triggers.json"

# Loaded at import time — module-level singletons
_config: dict = {}
CANONICAL_TRIGGERS: frozenset[str] = frozenset()
ALIASES: dict[str, str] = {}


def _load() -> None:
    global _config, CANONICAL_TRIGGERS, ALIASES
    with open(_CONFIG_PATH) as f:
        _config = json.load(f)
    CANONICAL_TRIGGERS = frozenset(_config["canonical_triggers"])
    ALIASES = {k.strip().lower(): v for k, v in _config["aliases"].items()}
    log.info(
        f"Trigger vocab loaded: {len(CANONICAL_TRIGGERS)} canonical, "
        f"{len(ALIASES)} aliases"
    )


def normalize_trigger(raw: str) -> str:
    """Lowercase, strip, resolve aliases, replace hyphens with underscores."""
    t = raw.strip().lower()
    # Resolve alias first (exact match on cleaned string)
    if t in ALIASES:
        return ALIASES[t]
    # Legacy hyphen→underscore compat
    t = t.replace("-", "_")
    if t in ALIASES:
        return ALIASES[t]
    return t


def is_known(trigger: str) -> bool:
    """Check if a (already-normalized) trigger is in the canonical set."""
    return trigger in CANONICAL_TRIGGERS


# Load on import
_load()
