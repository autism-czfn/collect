-- Migration 006: Trigger vocabulary normalization
-- 1. Normalize existing hyphenated triggers to snake_case
-- 2. Create unknown_triggers tracking table

-- Normalize "routine-change" → "routine_change" in existing rows
UPDATE mzhu_test_logs
SET triggers = array_replace(triggers, 'routine-change', 'routine_change')
WHERE 'routine-change' = ANY(triggers);

-- Table for tracking unknown triggers submitted by users
CREATE TABLE IF NOT EXISTS mzhu_test_unknown_triggers (
    trigger_text  TEXT PRIMARY KEY,
    count         INTEGER NOT NULL DEFAULT 1,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now()
);
