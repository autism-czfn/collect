-- 005_insights_cache.sql
-- Run once:
--   psql $USER_DATABASE_URL -f migrations/005_insights_cache.sql

CREATE TABLE IF NOT EXISTS mzhu_test_insights_cache (
  id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  days          INT         NOT NULL,
  insights_json JSONB       NOT NULL,
  generated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
