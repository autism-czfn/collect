# Collect — Autism Support Data Collection API

A FastAPI service that ingests caregiver observations about a child's day, transcribes spoken notes with Whisper, extracts structured fields via an LLM, and persists them to PostgreSQL for downstream review and weekly summarisation.

## What it does

- **Voice capture → structured data.** A caregiver records a short note. The server transcribes the audio with `faster-whisper`, asks `claude -p` to extract a fixed JSON shape, sanitises the result (clamping ratings, normalising triggers against the shared vocabulary, dropping unknown tags into `notes`), and writes an event log and/or a daily check-in row in a single transaction.
- **Event logs.** Free-form behaviour entries with triggers, context, response, outcome, severity (1–5), tags, intervention links, and soft-delete (`voided`). Responses include computed `time_of_day` and `environment` enrichment fields.
- **Trigger vocabulary.** A controlled vocabulary of 11 canonical triggers loaded from `config/triggers.json`, with alias resolution (e.g. "bad sleep" → "sleep"). Unknown triggers are accepted but generate warnings in the response and are tracked in `mzhu_test_unknown_triggers` for vocabulary expansion review.
- **Trigger signals.** Aggregated trigger data over a rolling window for the search repo to consume — frequency counts, severity averages, time-of-day distributions, and common contexts/environments.
- **Safety webhook.** Fire-and-forget notification to the search service when a safety-critical log is created (severity ≥ 4, self-harm/suicide/elopement/violence intent detected via regex). Configurable via `SEARCH_WEBHOOK_URL`; failures never block log creation.
- **Multilingual input.** Caregivers can speak in any language; the LLM extracts all structured fields in English. Original-language phrases are preserved in `raw_signals` for auditability.
- **Conversational chat.** A streaming chat tab backed by a two-stage pipeline: a planner LLM call classifies intent (`BEHAVIOR_PATTERN`, `INTERVENTION`, `MEDICAL`, `SAFETY`) and rewrites the query, then a parallel retrieval stage pulls relevant behaviour logs, crawled clinical evidence, and live search results before sending a streamed Claude answer over SSE. Conversation history is persisted per `child_id` in `mzhu_test_chat_messages` with automatic summarisation for long sessions.
- **Daily check-ins.** One row per day holding sparse 1–5 ratings (sleep, mood, sensory sensitivity, appetite, social tolerance, routine adherence, communication ease, physical activity, caregiver rating) plus a non-negative `meltdown_count` and notes. Repeat posts for the same date merge via JSONB concatenation.
- **Interventions.** Suggestions move through `open → adopted → closed` with outcome notes; soft-delete supported.
- **Weekly summaries.** Upsert-by-Monday `week_start` with free-form text and a stats JSONB blob; `GET /summaries/latest` returns the most recent.

## API surface

| Method | Path                          | Purpose                                  |
| ------ | ----------------------------- | ---------------------------------------- |
| GET    | `/health`                     | Liveness + loaded Whisper model name     |
| POST   | `/transcribe`                 | Audio → transcription only               |
| POST   | `/transcribe-and-log`         | Audio → transcription → LLM extract → DB |
| POST   | `/logs`                       | Create event log (returns `{log, warnings}`) |
| GET    | `/logs`                       | List recent logs (`days`, `limit`, `include_voided`) |
| GET    | `/logs/{id}`                  | Fetch one log                            |
| PUT    | `/logs/{id}`                  | Partial update (COALESCE semantics)      |
| PUT    | `/logs/{id}/void`             | Soft-delete                              |
| GET    | `/logs/trigger-signals`       | Enriched trigger signals for search repo (`days`, `child_id`) |
| GET    | `/triggers/vocabulary`        | Controlled trigger vocabulary + aliases  |
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
| POST   | `/api/chat/stream`            | SSE chat pipeline: planner → retrieval → Claude stream |
| GET    | `/api/chat/history`           | Load prior conversation turns for a `child_id` |

## Project layout

```
main.py                    FastAPI app, lifespan, /health, /transcribe
db.py                      asyncpg pool with JSONB codec (+ optional crawl pool)
models.py                  Pydantic request/response models + validators
trigger_vocab.py           Shared trigger vocabulary loader + normalizer
chat_planner.py            Intent classification + query rewrite (single LLM call)
chat_retrieval.py          Two-stage retrieval: logs, crawled evidence, live search
config/
  triggers.json            Canonical triggers + alias mappings
routes/
  logs.py                  /logs CRUD + void + trigger normalization
  interventions.py         /interventions lifecycle + void
  daily_checks.py          /daily-checks upsert-merge + reads
  summaries.py             /summaries upsert + latest
  transcribe_and_log.py    audio → Whisper → claude -p → DB
  triggers.py              /triggers/vocabulary endpoint
  trigger_signals.py       /logs/trigger-signals endpoint
  safety_webhook.py        Fire-and-forget safety webhook to search service
  chat.py                  /api/chat/stream (SSE) + /api/chat/history
migrations/
  001_create_tables.sql    logs, interventions, summaries
  002_create_daily_checks.sql
  003_extend_logs.sql
  004_clinician_cache.sql
  005_insights_cache.sql
  006_trigger_normalization.sql
  008_chat_messages.sql    chat_messages table + child_id/time index
setup.sh                   environment / dependency bootstrap
requirements.txt           Python dependencies
```

All tables are prefixed `mzhu_test_` (logs, interventions, summaries, daily_checks, unknown_triggers, chat_messages).

## Configuration

Reads `.env` from the project root:

| Variable             | Default   | Purpose                                    |
| -------------------- | --------- | ------------------------------------------ |
| `HOST`               | `0.0.0.0` | Bind address                               |
| `PORT`               | `18001`   | TLS port                                   |
| `WHISPER_MODEL`      | `base`    | faster-whisper model name                  |
| `WHISPER_LANGUAGE`   | auto      | Force transcription language               |
| `USER_DATABASE_URL`  | required  | asyncpg DSN; server exits if unset         |
| `SEARCH_WEBHOOK_URL` | *(none)*  | Safety webhook target (e.g. search service); disabled if unset |
| `SEARCH_SERVICE_URL` | *(none)*  | Live search service base URL used by chat retrieval; disabled if unset |
| `CRAWL_DATABASE_URL` | *(none)*  | asyncpg DSN for the crawled-evidence database; chat falls back to tsvector search or skips if unset |

TLS certs are expected at `../certs/cert.pem` and `../certs/key.pem` relative to this directory.

## Running

```bash
pip install -r requirements.txt
# apply migrations under migrations/ against USER_DATABASE_URL
python main.py
```

The `/transcribe-and-log` endpoint shells out to `claude -p` for field extraction, so the `claude` CLI must be on `PATH` and authenticated.

## Dependencies

`fastapi`, `uvicorn[standard]`, `python-multipart`, `python-dotenv`, `asyncpg`, `pydantic`, `faster-whisper`, `anthropic`, `httpx`, `fastembed`.
