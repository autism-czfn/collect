-- 003_extend_logs.sql
-- Run once:
--   psql $USER_DATABASE_URL -f migrations/003_extend_logs.sql

-- Add new columns to mzhu_test_logs
ALTER TABLE mzhu_test_logs
  ADD COLUMN IF NOT EXISTS child_id  TEXT,
  ADD COLUMN IF NOT EXISTS severity  SMALLINT CHECK (severity BETWEEN 1 AND 5),
  ADD COLUMN IF NOT EXISTS tags      TEXT[]   NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS notes     TEXT;

-- Relax NOT NULL on event and outcome so voice auto-save can write partial rows
ALTER TABLE mzhu_test_logs ALTER COLUMN event   DROP NOT NULL;
ALTER TABLE mzhu_test_logs ALTER COLUMN outcome DROP NOT NULL;

-- Remove the enum CHECK on outcome entirely — outcome is now free text
ALTER TABLE mzhu_test_logs DROP CONSTRAINT IF EXISTS mzhu_test_logs_outcome_check;
