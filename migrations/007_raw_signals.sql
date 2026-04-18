-- Add raw_signals column to preserve original user language/phrases
-- alongside normalized triggers. Required for safety auditability.
-- e.g. triggers=["self_harm"], raw_signals=["suicide", "不想活了"]

ALTER TABLE mzhu_test_logs
ADD COLUMN IF NOT EXISTS raw_signals text[] DEFAULT '{}';

COMMENT ON COLUMN mzhu_test_logs.raw_signals IS
  'Original phrases from user input before normalization. Preserved for trust, safety audit, and traceability.';
