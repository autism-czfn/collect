# Collect — Autism Support Data Collection API

A FastAPI service that ingests caregiver observations about a child's day, transcribes spoken notes with Whisper, extracts structured fields via an LLM, and persists them to PostgreSQL for downstream review and weekly summarisation.

## What it does

- **Voice capture → structured data.** A caregiver records a short note. The server transcribes the audio with `faster-whisper`, asks `claude -p` to extract a fixed JSON shape, sanitises the result (clamping ratings, dropping unknown triggers/tags into `notes`), and writes an event log and/or a daily check-in row in a single transaction.
- **Event logs.** Free-form behaviour entries with triggers, context, response, outcome, severity (1–5), tags, intervention links, and soft-delete (`voided`).
- **Daily check-ins.** One row per day holding sparse 1–5 ratings (sleep, mood, sensory sensitivity, appetite, social tolerance, routine adherence, communication ease, physical activity, caregiver rating) plus a non-negative `meltdown_count` and notes. Repeat posts for the same date merge via JSONB concatenation.
- **Interventions.** Suggestions move through `open → adopted → closed` with outcome notes; soft-delete supported.
- **Weekly summaries.** Upsert-by-Monday `week_start` with free-form text and a stats JSONB blob; `GET /summaries/latest` returns the most recent.

## API surface

| Method | Path                          | Purpose                                  |
| ------ | ----------------------------- | ---------------------------------------- |
| GET    | `/health`                     | Liveness + loaded Whisper model name     |
| POST   | `/transcribe`                 | Audio → transcription only               |
| POST   | `/transcribe-and-log`         | Audio → transcription → LLM extract → DB |
| POST   | `/logs`                       | Create event log                         |
| GET    | `/logs`                       | List recent logs (`days`, `limit`, `include_voided`) |
| GET    | `/logs/{id}`                  | Fetch one log                            |
| PUT    | `/logs/{id}`                  | Partial update (COALESCE semantics)      |
| PUT    | `/logs/{id}/void`             | Soft-delete                              |
| POST   | `/interventions`              | Create suggestion                        |
| GET    | `/interventions`              | List, filter by `status`                 |
| PUT    | `/interventions/{id}/adopt`   | `open → adopted`                         |
| PUT    | `/interventions/{id}/outcome` | `→ closed` with outcome note             |
| PUT    | `/interventions/{id}/void`    | Soft-delete                              |
| POST   | `/daily-checks`               | Upsert-merge for a given date            |
| GET    | `/daily-checks`               | List recent checks                       |
| GET    | `/daily-checks/{date}`        | Fetch one day                            |
| POST   | `/summaries`                  | Upsert weekly summary (Monday `week_start`) |
| GET    | `/summaries/latest`           | Most recent weekly summary               |

## Project layout

```
main.py                    FastAPI app, lifespan, /health, /transcribe
db.py                      asyncpg pool with JSONB codec
models.py                  Pydantic request/response models + validators
routes/
  logs.py                  /logs CRUD + void
  interventions.py         /interventions lifecycle + void
  daily_checks.py          /daily-checks upsert-merge + reads
  summaries.py             /summaries upsert + latest
  transcribe_and_log.py    audio → Whisper → claude -p → DB
migrations/
  001_create_tables.sql    logs, interventions, summaries
  002_create_daily_checks.sql
  003_extend_logs.sql
  004_clinician_cache.sql
  005_insights_cache.sql
setup.sh                   environment / dependency bootstrap
requirements.txt           Python dependencies
```

All tables are prefixed `mzhu_test_` (logs, interventions, summaries, daily_checks).

## Configuration

Reads `.env` from the project root:

| Variable             | Default   | Purpose                                    |
| -------------------- | --------- | ------------------------------------------ |
| `HOST`               | `0.0.0.0` | Bind address                               |
| `PORT`               | `18001`   | TLS port                                   |
| `WHISPER_MODEL`      | `base`    | faster-whisper model name                  |
| `WHISPER_LANGUAGE`   | auto      | Force transcription language               |
| `USER_DATABASE_URL`  | required  | asyncpg DSN; server exits if unset         |

TLS certs are expected at `../certs/cert.pem` and `../certs/key.pem` relative to this directory.

## Running

```bash
pip install -r requirements.txt
# apply migrations under migrations/ against USER_DATABASE_URL
python main.py
```

The `/transcribe-and-log` endpoint shells out to `claude -p` for field extraction, so the `claude` CLI must be on `PATH` and authenticated.

## Dependencies

`fastapi`, `uvicorn[standard]`, `python-multipart`, `python-dotenv`, `asyncpg`, `pydantic`, `faster-whisper`, `anthropic`.
