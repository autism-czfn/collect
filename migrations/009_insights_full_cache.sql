-- 009_insights_full_cache.sql
-- Cache table for /api/insights/full (evidence + LLM recommendations).
-- Run once:
--   psql $USER_DATABASE_URL -f migrations/009_insights_full_cache.sql

CREATE TABLE IF NOT EXISTS mzhu_test_insights_full_cache (
  id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  days          INTEGER     NOT NULL DEFAULT 30,
  response_json JSONB       NOT NULL,
  generated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
