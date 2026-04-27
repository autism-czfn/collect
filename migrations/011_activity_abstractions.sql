CREATE TABLE mzhu_test_activity_abstractions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    child_id            TEXT NOT NULL DEFAULT 'default',
    log_date            DATE NOT NULL,
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_ids          JSONB,          -- {log_ids:[], note_ids:[], food_log_ids:[]}
    categories          JSONB,          -- flattened LLM output: {overall_day_quality, meltdowns, ...}
    llm_summary         TEXT,           -- 2-3 sentence narrative (LLM-produced)
    raw_sentences_kept  TEXT[],         -- verbatim sentences from mzhu_test_voice_notes
                                        -- extracted in Python BEFORE LLM call, NOT from LLM
    user_corrections    JSONB,          -- {dotted.path: {original, corrected, corrected_at}}
    version             INT NOT NULL DEFAULT 1,
    is_current          BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE UNIQUE INDEX ON mzhu_test_activity_abstractions (child_id, log_date)
    WHERE is_current = TRUE;

CREATE INDEX ON mzhu_test_activity_abstractions (child_id, log_date DESC);
