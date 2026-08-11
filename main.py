"""
Royal Road TTS Web App

A self-hosted web app that tracks Royal Road web novels,
synthesizes chapter audio with Kokoro TTS, and streams
playback with progress tracking.

Usage:
    python main.py
    python main.py --port 8080
    python main.py --host 127.0.0.1 --port 3000
"""

import argparse
import json
import logging
import os
import subprocess
import yaml
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

import cache_policy
from database import init_db, SessionLocal
from routers import (novels, chapters, progress, settings, exports, epubs,
                     characters, text_rules_api)
from tts import cleanup_temp_files

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Chatterbox runs tqdm progress bars over every generation (hundreds of lines
# of carriage-returned sampling rates per chunk). On a headless server they
# only bury the log lines that matter. tqdm >= 4.66 honors this for any bar
# that doesn't pass disable explicitly, which is all of chatterbox's.
os.environ.setdefault("TQDM_DISABLE", "1")


class _ProactorDisconnectFilter(logging.Filter):
    """Drop Windows' phantom disconnect errors from the log.

    The Proactor event loop logs a full ERROR traceback every time a client
    vanishes mid-transfer — a phone locking, Safari abandoning a segment
    fetch. That is routine on a server whose only client is a phone: one
    evening of use logged 168 of them, burying the errors that matter.
    """
    def filter(self, record):
        return "_call_connection_lost" not in record.getMessage()


logging.getLogger("asyncio").addFilter(_ProactorDisconnectFilter())


def _git_sha() -> str:
    """Short SHA of the running code, so you can tell what's actually live
    (the tray launches main.py as a child — easy to run a stale build)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent, capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip() or "unknown"
    except Exception:
        pass
    return "unknown"


APP_VERSION = _git_sha()
STARTED_AT: datetime | None = None


def _retention_cleanup():
    """Apply the retention policy: the reading window kept, the few chapters
    just behind it kept while fresh, everything else deleted (see
    cache_policy.retention_sets)."""
    db = SessionLocal()
    try:
        keep, expiring = cache_policy.retention_sets(db)
    finally:
        db.close()
    cleanup_temp_files(keep, expiring)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup, clean temp files on startup/shutdown."""
    global STARTED_AT
    STARTED_AT = datetime.now(timezone.utc)
    logger.info("Initializing database...")
    init_db()
    logger.info("Applying audio cache retention policy...")
    _retention_cleanup()
    import export_worker
    export_worker.start_worker()
    import prefetch
    prefetch.start_worker()
    import epub_library
    epub_library.start()
    logger.info("Novel TTS server ready (version=%s, started %s).",
                APP_VERSION, STARTED_AT.isoformat())
    yield
    prefetch.stop()
    epub_library.stop()
    logger.info("Shutting down — applying audio cache retention policy...")
    _retention_cleanup()


app = FastAPI(
    title="Novel TTS",
    description="Listen to web novels with AI-generated narration",
    version="1.0.0",
    lifespan=lifespan,
)

@app.middleware("http")
async def no_cache_frontend(request, call_next):
    """
    Phone browsers (Safari especially) heuristically cache static assets,
    serving stale app.js/index.html after updates. no-cache forces ETag
    revalidation — repeat loads stay cheap on LAN (304s).
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static") or path.endswith(".m3u8"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# Mount API routers
app.include_router(novels.router)
app.include_router(chapters.router)
app.include_router(progress.router)
app.include_router(settings.router)
app.include_router(exports.router)
app.include_router(epubs.router)
app.include_router(characters.router)
app.include_router(text_rules_api.router)

# Serve frontend static files
FRONTEND_DIR = Path(__file__).parent / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def serve_index():
    """
    Serve the frontend SPA with version-stamped asset URLs (mtime-based), so
    a browser holding stale cached app.js/style.css is forced to re-fetch
    them whenever they change — phone Safari ignores freshness headers it
    never saw when it first cached an asset.
    """
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    version = int(max(
        (FRONTEND_DIR / name).stat().st_mtime for name in ("app.js", "style.css")
    ))
    return HTMLResponse(html.replace("__V__", str(version)))


@app.get("/api/version")
async def version():
    """Running code's git SHA and process start time — so you can confirm
    which build is actually live (the tray runs main.py as a child)."""
    return {
        "git_sha": APP_VERSION,
        "started_at": STARTED_AT.isoformat() if STARTED_AT else None,
    }


# Diagnostic event stream from the frontend (iOS lock-screen debugging: the
# phone can't be inspected directly, so the page reports what it saw). JSONL,
# one event per line, client timestamps preserved.
CLIENT_LOG = Path(__file__).parent / "client_events.log"


@app.post("/api/client-log")
async def client_log(request: Request):
    try:
        payload = json.loads(await request.body())
    except Exception:
        return {"ok": False}
    events = payload.get("events", [])
    if not isinstance(events, list):
        return {"ok": False}
    sid = str(payload.get("sid", ""))[:16]
    received = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with CLIENT_LOG.open("a", encoding="utf-8") as f:
        for ev in events[:500]:
            if isinstance(ev, dict):
                f.write(json.dumps({"srv": received, "sid": sid, **ev},
                                   ensure_ascii=False, default=str) + "\n")
    return {"ok": True}


@app.post("/api/library/refresh-favorites")
async def refresh_favorites():
    """Kick the background favorites sync (called by the frontend on load)."""
    import library_sync
    return library_sync.start_refresh()


@app.get("/api/scrapers")
async def list_scrapers():
    """Supported sites, straight from the scraper registry — never hardcoded."""
    from scrapers import discover_scrapers
    return {"scrapers": [
        {"name": s.name, "patterns": [p.pattern for p in s.url_patterns]}
        for s in discover_scrapers()
    ]}


@app.get("/api/library/sync-status")
async def library_sync_status():
    """Whether the favorites sync is still running — the frontend polls this
    after kicking a refresh so it can re-render unread counts when new
    chapters land."""
    import library_sync
    return {"running": library_sync.is_running()}


@app.get("/api/engines")
async def list_engines():
    """Available TTS engines and their voices, for the model/voice pickers."""
    import engines as engine_registry

    out = []
    for name, engine in engine_registry.discover_engines().items():
        ok, reason = engine.available()
        out.append({
            "name": name,
            "label": engine.label,
            "available": ok,
            "unavailable_reason": reason,
            "supports_speed": engine.supports_speed,
            "supports_custom_voices": any(v.custom for v in engine.voices()) or name == "chatterbox",
            "default_voice": engine.default_voice(),
            "supports_voice_settings": engine.supports_voice_settings(),
            "voices": [
                {"id": v.id, "label": v.label, "custom": v.custom,
                 "settings": engine.voice_settings(v.id)}
                for v in engine.voices()
            ],
        })
    out.sort(key=lambda e: e["name"] != engine_registry.DEFAULT_ENGINE)
    return {"engines": out, "default_engine": engine_registry.DEFAULT_ENGINE}


@app.get("/api/voices")
async def list_voices(engine: str | None = None):
    """Voices for one engine (defaults to the configured one).

    Kept at its original path and shape so existing callers keep working; the
    voice list is now sourced from the engine rather than config.yaml directly.
    """
    import engines as engine_registry
    from database import SessionLocal, Settings

    if engine is None:
        db = SessionLocal()
        try:
            row = db.query(Settings).first()
            engine = (row.engine if row else None) or engine_registry.DEFAULT_ENGINE
        finally:
            db.close()

    impl = engine_registry.get_engine(engine)
    return {
        "engine": impl.name,
        "voices": [{"id": v.id, "label": v.label, "custom": v.custom} for v in impl.voices()],
        "default_voice": impl.default_voice(),
        "default_speed": 1.0,
        "supports_speed": impl.supports_speed,
    }


# Same passage for every voice so demos are directly comparable.
DEMO_TEXT = (
    "Chapter one. The rain had stopped by the time Simon reached the old library, "
    "but thunder still rolled somewhere beyond the hills. "
    "\"You're late,\" the archivist said, not looking up from her ledger."
)
VOICE_DEMO_DIR = Path(__file__).parent / "voice_demos"


class VoiceSettingsRequest(BaseModel):
    sentence_pause: float | None = None


@app.patch("/api/voices/{voice_id}/settings")
async def update_voice_settings(voice_id: str, req: VoiceSettingsRequest,
                                engine: str | None = None):
    """Tune one voice. Cached audio for novels using it is dropped, since the
    pause is baked into the rendered files rather than applied at playback."""
    import engines as engine_registry
    from database import SessionLocal, Novel, Settings, effective_settings
    from tts import remove_chapter_audio

    impl = engine_registry.get_engine(engine)
    if impl.resolve_voice(voice_id) is None:
        raise HTTPException(status_code=404, detail="Unknown voice")
    if not impl.supports_voice_settings():
        raise HTTPException(status_code=400,
                            detail=f"{impl.label} has no per-voice settings")

    values = {k: v for k, v in req.model_dump().items() if v is not None}
    if not values:
        return {"voice": voice_id, "settings": impl.voice_settings(voice_id)}
    try:
        settings = impl.set_voice_settings(voice_id, **values)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # The demo clip is cached per voice, so drop it too or the preview would
    # keep playing the old pause and the setting would look like it did nothing.
    (VOICE_DEMO_DIR / f"{voice_id}.wav").unlink(missing_ok=True)

    db = SessionLocal()
    try:
        globals_row = db.query(Settings).first()
        stale = set()
        for novel in db.query(Novel).all():
            eff = effective_settings(novel, globals_row)
            if eff["engine"] == impl.name and eff["voice"] == voice_id:
                stale.update(ch.id for ch in novel.chapters)
        if stale:
            remove_chapter_audio(stale)
            logger.info("Voice %s retuned — dropped audio for %d chapter(s)",
                        voice_id, len(stale))
    finally:
        db.close()

    return {"voice": voice_id, "settings": settings}


@app.get("/api/voices/{voice_id}/demo")
async def voice_demo(voice_id: str, engine: str | None = None):
    """Serve a short demo clip for a voice; synthesized on first request, cached on disk.

    Runs at interactive priority — the user is actively waiting to hear it.
    Voice ids are namespaced per engine (Kokoro's af_*/bm_*, Chatterbox's cb_*),
    so demo filenames stay unique without an engine prefix — which also keeps
    the clips already on disk valid.
    """
    import engines as engine_registry

    impl = engine_registry.get_engine(engine)
    if impl.resolve_voice(voice_id) is None:
        raise HTTPException(status_code=404, detail="Unknown voice")

    demo_path = VOICE_DEMO_DIR / f"{voice_id}.wav"
    if not demo_path.exists():
        import numpy as np
        import soundfile as sf
        import tts
        with tts.interactive_synthesis():
            segments = await tts.synthesize_batch(DEMO_TEXT, voice_id, 1.0, impl.name)
        if not segments:
            raise HTTPException(status_code=502, detail="Demo synthesis produced no audio")
        VOICE_DEMO_DIR.mkdir(exist_ok=True)
        sf.write(str(demo_path), np.concatenate(segments), impl.sample_rate, subtype="PCM_16")
    return FileResponse(str(demo_path), media_type="audio/wav", filename=f"{voice_id}.wav")


def main():
    parser = argparse.ArgumentParser(description="Royal Road TTS Web App")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to (default: 8000)")
    args = parser.parse_args()

    print(f"""
========================================
  Novel TTS Server
  Powered by Kokoro TTS + CUDA GPU
  v1.0.0
========================================

  Listening on: http://{args.host}:{args.port}
  Local access: http://localhost:{args.port}
""")

    uvicorn.run(
        "main:app",
        host=args.host,
        port=args.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
