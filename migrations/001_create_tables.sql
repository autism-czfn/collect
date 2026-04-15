-- 001_create_tables.sql
-- Run once on first install:
--   psql $USER_DATABASE_URL -f migrations/001_create_tables.sql

CREATE TABLE IF NOT EXISTS mzhu_test_logs (
  id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  logged_at        TIMESTAMPTZ NOT NULL    DEFAULT now(),
  event            TEXT        NOT NULL,
  triggers         TEXT[]      NOT NULL    DEFAULT '{}',
  context          TEXT,
  response         TEXT,
  outcome          TEXT        NOT NULL
                     CHECK (outcome IN (
                       'calm', 'mild_distress', 'meltdown',
                       'regression', 'positive'
                     )),
  intervention_ids UUID[]      NOT NULL    DEFAULT '{}',
  voided           BOOLEAN     NOT NULL    DEFAULT false,
  voided_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS mzhu_test_logs_logged_at_idx
  ON mzhu_test_logs (logged_at DESC);

CREATE INDEX IF NOT EXISTS mzhu_test_logs_triggers_idx
  ON mzhu_test_logs USING GIN (triggers);

CREATE TABLE IF NOT EXISTS mzhu_test_interventions (
  id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  suggestion_text  TEXT        NOT NULL,
  category         TEXT,
  suggested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at       TIMESTAMPTZ,
  status           TEXT        NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open', 'adopted', 'closed')),
  outcome_note     TEXT,
  closed_at        TIMESTAMPTZ,
  voided           BOOLEAN     NOT NULL DEFAULT false,
  voided_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS mzhu_test_interventions_status_idx
  ON mzhu_test_interventions (status, suggested_at DESC);

CREATE TABLE IF NOT EXISTS mzhu_test_summaries (
  id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  week_start    DATE        NOT NULL UNIQUE,
  summary_text  TEXT        NOT NULL,
  stats_json    JSONB       NOT NULL,
  generated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
