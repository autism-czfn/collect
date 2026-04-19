-- 008_create_user_settings.sql  (P-COL-6)
-- Run once:
--   psql $USER_DATABASE_URL -f migrations/008_create_user_settings.sql

CREATE TABLE IF NOT EXISTS mzhu_test_user_settings (
  id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id            TEXT        NOT NULL,
  child_id           TEXT        NOT NULL,
  timezone           TEXT,
  language           TEXT,
  child_display_name TEXT,
  ui_preferences     JSONB,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, child_id)
);

COMMENT ON TABLE mzhu_test_user_settings IS
  'Per-caregiver + per-child settings. Single-user deployment uses user_id=default, child_id=default.';

COMMENT ON COLUMN mzhu_test_user_settings.ui_preferences IS
  'JSONB blob for UI state (e.g. audience toggle). Merged on partial POST via COALESCE.';
