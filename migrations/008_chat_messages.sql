-- Migration 008: chat_messages table
-- Stores conversation turns for /api/chat/stream.
-- child_id (UUID from client localStorage) is the session key — no sessions table.

CREATE TABLE IF NOT EXISTS mzhu_test_chat_messages (
    id          BIGSERIAL    PRIMARY KEY,
    child_id    TEXT         NOT NULL,
    role        TEXT         NOT NULL,   -- 'user' | 'assistant' | 'summary'
    content     TEXT         NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_child_time
    ON mzhu_test_chat_messages (child_id, created_at DESC);
