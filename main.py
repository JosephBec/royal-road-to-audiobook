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
import logging
import yaml
from pathlib import Path

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from database import init_db, SessionLocal, retention_policy
from routers import novels, chapters, progress, settings
from tts import cleanup_temp_files

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _retention_cleanup():
    """Apply the retention policy: in-progress chapters and favorites' next-3
    kept forever, non-favorites' next-3 kept while fresh, the rest deleted."""
    db = SessionLocal()
    try:
        forever, expiring = retention_policy(db)
    finally:
        db.close()
    cleanup_temp_files(forever, expiring)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup, clean temp files on startup/shutdown."""
    logger.info("Initializing database...")
    init_db()
    logger.info("Applying audio cache retention policy...")
    _retention_cleanup()
    logger.info("Novel TTS server ready.")
    yield
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


@app.post("/api/library/refresh-favorites")
async def refresh_favorites():
    """Kick the background favorites sync (called by the frontend on load)."""
    import library_sync
    return library_sync.start_refresh()


@app.get("/api/voices")
async def list_voices():
    """List available voices from config.yaml."""
    config_path = Path(__file__).parent / "config.yaml"
    if config_path.exists():
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
        return {
            "voices": config.get("voices", []),
            "default_voice": config.get("default_voice", "af_heart"),
            "default_speed": config.get("default_speed", 1.0),
        }
    return {
        "voices": [{"id": "af_heart", "label": "Heart (Female, American)"}],
        "default_voice": "af_heart",
        "default_speed": 1.0,
    }


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
