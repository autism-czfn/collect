#!/usr/bin/env python3
"""
Autism Support App — Data Collection API
  POST /transcribe          voice transcription (existing)
  GET  /health              liveness check (existing)
  /logs                     log CRUD + soft-delete (new)
  /interventions            intervention CRUD + soft-delete (new)
  /summaries                weekly summary storage (new)
"""
import os
import socket
import tempfile
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ── Load config ────────────────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

HOST          = os.getenv("HOST", "0.0.0.0")
PORT          = int(os.getenv("PORT", "18001"))
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
WHISPER_LANG  = os.getenv("WHISPER_LANGUAGE") or None

CERT_DIR = Path(__file__).parent.parent / "certs"
CERT     = CERT_DIR / "cert.pem"
KEY      = CERT_DIR / "key.pem"

logging.basicConfig(level=logging.INFO, format="  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ── DB pool + Whisper lifespan ─────────────────────────────────────────────────
import db as _db
from faster_whisper import WhisperModel


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _db.create_pool()
    await _db.create_crawl_pool()          # optional — logs warning if not configured
    log.info(f"Loading faster-whisper model '{WHISPER_MODEL}' …")
    app.state.whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    log.info("Model ready.")
    yield
    await _db.close_pool()
    await _db.close_crawl_pool()


# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Autism Support — Collect API",
    version="2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ────────────────────────────────────────────────────────────────────
from routes.logs import router as logs_router
from routes.interventions import router as interventions_router
from routes.summaries import router as summaries_router
from routes.daily_checks import router as daily_checks_router
from routes.transcribe_and_log import router as tal_router
from routes.triggers import router as triggers_router
from routes.trigger_signals import router as trigger_signals_router
from routes.chat import router as chat_router
from routes.food_log import router as food_log_router
from routes.voice_notes import router as voice_notes_router
from routes.abstractions import router as abstractions_router

# trigger_signals_router MUST be registered before logs_router:
# /logs/trigger-signals (static) must match before /logs/{log_id} (parameterized),
# otherwise FastAPI returns 422 trying to parse "trigger-signals" as UUID.
app.include_router(trigger_signals_router)
app.include_router(logs_router)
app.include_router(interventions_router)
app.include_router(summaries_router)
app.include_router(daily_checks_router)
app.include_router(tal_router)
app.include_router(triggers_router)
app.include_router(chat_router)
app.include_router(food_log_router)
app.include_router(voice_notes_router)
app.include_router(abstractions_router)

# ── Existing endpoints (unchanged) ─────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "model": WHISPER_MODEL}


@app.post("/transcribe")
async def transcribe(request: Request, audio: UploadFile = File(...)):
    """
    Accept an audio file (webm, ogg, wav, mp4, m4a …) and return transcribed text.
    ffmpeg handles all format conversions automatically.
    """
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty audio file")

    suffix = Path(audio.filename or "recording.webm").suffix or ".webm"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        whisper = request.app.state.whisper_model
        segments, info = whisper.transcribe(
            tmp_path,
            language=WHISPER_LANG,
            beam_size=5,
            vad_filter=True,
        )
        text = " ".join(s.text for s in segments).strip()
        log.info(f"Transcribed {info.duration:.1f}s → '{text[:60]}{'…' if len(text)>60 else ''}'")
        return {
            "text":     text,
            "language": info.language,
            "duration": round(info.duration, 2),
        }
    except Exception as e:
        log.error(f"Transcription error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        os.unlink(tmp_path)


def get_local_ip() -> str:
    """Return the machine's outbound LAN IP (never 0.0.0.0 or loopback)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    display_host = get_local_ip() if HOST in ("0.0.0.0", "") else HOST
    log.info(f"Collect API → https://{display_host}:{PORT}/")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning",
                ssl_certfile=str(CERT), ssl_keyfile=str(KEY))
