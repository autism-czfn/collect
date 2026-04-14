#!/usr/bin/env python3
"""
Voice transcription API using faster-whisper.
POST /transcribe  — accepts an audio file, returns transcribed text.
GET  /health      — liveness check.
"""
import os
import socket
import tempfile
import logging
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ── Load config ────────────────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

HOST            = os.getenv("HOST", "0.0.0.0")
PORT            = int(os.getenv("PORT", "18001"))
WHISPER_MODEL   = os.getenv("WHISPER_MODEL", "base")
WHISPER_LANG    = os.getenv("WHISPER_LANGUAGE") or None   # None = auto-detect

CERT_DIR = Path(__file__).parent.parent / "certs"
CERT     = CERT_DIR / "cert.pem"
KEY      = CERT_DIR / "key.pem"


logging.basicConfig(level=logging.INFO, format="  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ── Load Whisper model once at startup ─────────────────────────────────────────
log.info(f"Loading faster-whisper model '{WHISPER_MODEL}' …")
from faster_whisper import WhisperModel
model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
log.info("Model ready.")

# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(title="Voice Transcription API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "model": WHISPER_MODEL}


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    """
    Accept an audio file (webm, ogg, wav, mp4, m4a …) and return transcribed text.
    ffmpeg handles all format conversions automatically.
    """
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty audio file")

    # Preserve original extension so ffmpeg picks the right demuxer
    suffix = Path(audio.filename or "recording.webm").suffix or ".webm"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        segments, info = model.transcribe(
            tmp_path,
            language=WHISPER_LANG,
            beam_size=5,
            vad_filter=True,          # skip silence automatically
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
            s.connect(("1.1.1.1", 80))   # no packet sent; just resolves route
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    display_host = get_local_ip() if HOST in ("0.0.0.0", "") else HOST
    log.info(f"Transcription API → https://{display_host}:{PORT}/")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning",
                ssl_certfile=str(CERT), ssl_keyfile=str(KEY))
