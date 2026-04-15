CREATE TABLE mzhu_test_daily_checks (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  check_date  DATE NOT NULL UNIQUE,
  ratings     JSONB NOT NULL,
  notes       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON mzhu_test_daily_checks (check_date DESC);
