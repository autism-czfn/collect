# Collect — Autism Support Data Collection API

A FastAPI service that ingests caregiver observations about a child's day, transcribes spoken notes with Whisper, extracts structured fields via an LLM, and persists them to PostgreSQL for downstream review and weekly summarisation.

## What it does

- **Voice capture → structured data.** A caregiver records a short note. The server transcribes the audio with `faster-whisper`, asks `claude -p` to extract a fixed JSON shape, sanitises the result (clamping ratings, normalising triggers against the shared vocabulary, dropping unknown tags into `notes`), and writes an event log and/or a daily check-in row in a single transaction.
- **Event logs.** Free-form behaviour entries with triggers, raw signals (original-language phrases), context, response, outcome, severity (1–5), tags, intervention links, and soft-delete (`voided`). Responses include computed `time_of_day` and `environment` enrichment fields.
- **Trigger vocabulary.** A controlled vocabulary of 16 canonical triggers loaded from `config/triggers.json`, with alias resolution (e.g. "bad sleep" → "sleep", "暴力" → "aggression"). Unknown triggers are accepted but generate warnings in the response and are tracked in `mzhu_test_unknown_triggers` for vocabulary expansion review.
- **Admin vocabulary management.** Unknown triggers accumulate in a ranked frequency table. Admins can promote them to canonical triggers or aliases via `POST /admin/unknown-triggers/{text}/promote`; the vocabulary reloads immediately in-process.
- **Trigger signals.** Aggregated trigger data over a rolling window for the search repo to consume — frequency counts, severity averages, time-of-day distributions, and common contexts/environments.
- **Safety webhook.** Fire-and-forget notification to the search service when a safety-critical log is created (severity ≥ 4, self-harm/suicide/elopement/violence intent detected via semantic regex). Retried up to 3× with exponential backoff (1 s → 2 s → 4 s). Configurable via `SEARCH_WEBHOOK_URL`; failures never block log creation.
- **Multilingual input.** Caregivers can speak in any language; the LLM extracts all structured fields in English. Original-language phrases are preserved in `raw_signals` for auditability.
- **Transcribe-and-log confirmation flow (P-UI-3).** `POST /transcribe-and-log` returns extracted fields with confidence scores for caregiver review — **it does not write to the DB**. The UI must confirm and separately `POST /logs` with the reviewed payload. Rate-limited to 10 requests / 60 s per client IP.
- **Daily check-ins.** One row per day holding sparse 1–5 ratings (sleep, mood, sensory sensitivity, appetite, social tolerance, routine adherence, communication ease, physical activity, caregiver rating) plus a non-negative `meltdown_count` and notes. Repeat posts for the same date merge via JSONB concatenation.
- **Interventions.** Suggestions move through `open → adopted → closed` with outcome notes; soft-delete supported.
- **Weekly summaries.** Upsert-by-Monday `week_start` with free-form text and a stats JSONB blob; `GET /summaries/latest` returns the most recent.
- **User settings.** Per-(user_id, child_id) caregiver preferences: timezone, language, child display name, and arbitrary UI state in a JSONB blob. `GET /user-settings` returns 200 with nulls if no record exists yet; `POST /user-settings` upserts with COALESCE semantics so omitted fields are preserved.

## API surface

| Method | Path                                        | Purpose                                                        |
| ------ | ------------------------------------------- | -------------------------------------------------------------- |
| GET    | `/health`                                   | Liveness + loaded Whisper model name                           |
| POST   | `/transcribe`                               | Audio → transcription only                                     |
| POST   | `/transcribe-and-log`                       | Audio → transcription → LLM extract → UI review (no DB write) |
| POST   | `/logs`                                     | Create event log (returns `{log, warnings}`)                   |
| GET    | `/logs`                                     | List recent logs (`days`, `limit`, `offset`, `include_voided`) |
| GET    | `/logs/{id}`                                | Fetch one log                                                  |
| PUT    | `/logs/{id}`                                | Partial update (COALESCE semantics, trigger normalisation)     |
| PUT    | `/logs/{id}/void`                           | Soft-delete                                                    |
| GET    | `/logs/trigger-signals`                     | Enriched trigger signals for search repo (`days`, `child_id`)  |
| GET    | `/triggers/vocabulary`                      | Controlled trigger vocabulary + aliases                        |
| POST   | `/interventions`                            | Create suggestion                                              |
| GET    | `/interventions`                            | List, filter by `status`                                       |
| PUT    | `/interventions/{id}/adopt`                 | `open → adopted`                                               |
| PUT    | `/interventions/{id}/outcome`               | `→ closed` with outcome note                                   |
| PUT    | `/interventions/{id}/void`                  | Soft-delete                                                    |
| POST   | `/daily-checks`                             | Upsert-merge for a given date                                  |
| GET    | `/daily-checks`                             | List recent checks                                             |
| GET    | `/daily-checks/{date}`                      | Fetch one day                                                  |
| POST   | `/summaries`                                | Upsert weekly summary (Monday `week_start`)                    |
| GET    | `/summaries/latest`                         | Most recent weekly summary                                     |
| GET    | `/user-settings`                            | Retrieve caregiver settings (200 with nulls if not found)      |
| POST   | `/user-settings`                            | Create or upsert caregiver settings                            |
| GET    | `/admin/unknown-triggers`                   | List unknown triggers ranked by frequency                      |
| POST   | `/admin/unknown-triggers/{text}/promote`    | Promote trigger to canonical or alias; reloads vocab in-process |

## Project layout

```
main.py                    FastAPI app, lifespan, /health, /transcribe
db.py                      asyncpg pool with JSONB codec
models.py                  Pydantic request/response models + validators
trigger_vocab.py           Shared trigger vocabulary loader + normalizer
config/
  triggers.json            Canonical triggers + alias mappings (16 canonical, 50+ aliases)
routes/
  logs.py                  /logs CRUD + void + trigger normalisation + pagination
  interventions.py         /interventions lifecycle + void
  daily_checks.py          /daily-checks upsert-merge + reads
  summaries.py             /summaries upsert + latest
  transcribe_and_log.py    audio → Whisper → claude -p → confidence-scored extraction (no DB write)
  triggers.py              /triggers/vocabulary endpoint
  trigger_signals.py       /logs/trigger-signals endpoint
  safety_webhook.py        Fire-and-forget safety webhook with 3× retry + exponential backoff
  user_settings.py         /user-settings CRUD (upsert with COALESCE semantics)
  admin.py                 /admin/unknown-triggers vocabulary management
migrations/
  001_create_tables.sql    logs, interventions, summaries
  002_create_daily_checks.sql
  003_extend_logs.sql
  004_clinician_cache.sql
  005_insights_cache.sql
  006_trigger_normalization.sql
  007_raw_signals.sql      add raw_signals TEXT[] column to logs
  008_create_user_settings.sql
  009_insights_full_cache.sql
setup.sh                   environment / dependency bootstrap
requirements.txt           Python dependencies
```

All tables are prefixed `mzhu_test_` (logs, interventions, summaries, daily_checks, unknown_triggers, user_settings, insights_full_cache).

## Configuration

Reads `.env` from the project root:

| Variable             | Default   | Purpose                                                         |
| -------------------- | --------- | --------------------------------------------------------------- |
| `HOST`               | `0.0.0.0` | Bind address                                                    |
| `PORT`               | `18001`   | TLS port                                                        |
| `WHISPER_MODEL`      | `base`    | faster-whisper model name                                       |
| `WHISPER_LANGUAGE`   | auto      | Force transcription language                                    |
| `USER_DATABASE_URL`  | required  | asyncpg DSN; server exits if unset                              |
| `SEARCH_WEBHOOK_URL` | *(none)*  | Safety webhook target (e.g. search service); disabled if unset  |

TLS certs are expected at `../certs/cert.pem` and `../certs/key.pem` relative to this directory.

## Running

```bash
pip install -r requirements.txt
# apply migrations under migrations/ against USER_DATABASE_URL
python main.py
```

The `/transcribe-and-log` endpoint shells out to `claude -p` for field extraction, so the `claude` CLI must be on `PATH` and authenticated.

## Dependencies

`fastapi`, `uvicorn[standard]`, `python-multipart`, `python-dotenv`, `asyncpg`, `pydantic`, `faster-whisper`, `anthropic`, `httpx`.
