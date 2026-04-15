-- 004_clinician_cache.sql
-- Run once:
--   psql $USER_DATABASE_URL -f migrations/004_clinician_cache.sql

CREATE TABLE IF NOT EXISTS mzhu_test_clinician_cache (
  id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  report_json   JSONB       NOT NULL,
  generated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
