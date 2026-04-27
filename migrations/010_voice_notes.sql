CREATE TABLE mzhu_test_voice_notes (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    child_id            TEXT NOT NULL DEFAULT 'default',
    logged_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    client_local_hour   SMALLINT,                       -- 0-23, from client new Date().getHours()
    local_time_label    TEXT,                           -- morning/afternoon/evening/night
    raw_text            TEXT NOT NULL,                  -- full Whisper transcript
    sentences           TEXT[],                         -- split on sentence boundaries
    preliminary_category TEXT[],                        -- rule-based tags (note-level union)
    user_edited_text    TEXT,                           -- if user corrected the transcription
    user_edited_at      TIMESTAMPTZ,
    voided              BOOLEAN NOT NULL DEFAULT FALSE,
    voided_at           TIMESTAMPTZ
);

CREATE INDEX ON mzhu_test_voice_notes (child_id, logged_at DESC);
