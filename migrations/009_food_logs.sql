CREATE TABLE mzhu_test_food_logs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    child_id            TEXT NOT NULL DEFAULT 'default',
    logged_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    client_local_hour   SMALLINT,                       -- 0-23, from client new Date().getHours()
    meal_type           TEXT,                           -- breakfast/lunch/snack/dinner/late_night
    photo_data          BYTEA,                          -- raw image bytes (Option A — active)
    -- photo_path       TEXT,                           -- Option B: enable when migrating to disk
    photo_mime          TEXT NOT NULL DEFAULT 'image/jpeg',
    foods_identified    TEXT[],
    estimated_calories  INT,                            -- validated to [0, 5000] or NULL
    macros              JSONB,                          -- {protein_g, carbs_g, fat_g, fiber_g}
    sensory_notes       TEXT,
    concerns            TEXT,
    confidence          TEXT,                           -- high/medium/low
    user_notes          TEXT,                           -- free text typed alongside the photo
    voided              BOOLEAN NOT NULL DEFAULT FALSE,
    voided_at           TIMESTAMPTZ
);

CREATE INDEX ON mzhu_test_food_logs (child_id, logged_at DESC);
CREATE INDEX ON mzhu_test_food_logs (child_id, meal_type);
