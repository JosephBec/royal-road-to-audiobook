"""Save-to-Plex export job engine.

Jobs synthesize chapter ranges to M4B on the shared TTS worker at strictly
lowest priority: between every ~600-word batch the worker yields until
playback is idle, active listeners' prefetch is warm, and favorites sync
is done. Artifacts live in export_jobs/<id>/ (survives restarts; retry
resumes by skipping finished chapter WAVs).
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from database import SessionLocal, Chapter, Progress
import library_sync
import tts

logger = logging.getLogger(__name__)

EXPORT_DIR = Path("./export_jobs")
HEARTBEAT_WINDOW = 90  # seconds: progress update newer than this = active listener
PREFETCH_DEPTH = 3

_cancel_requested: set[int] = set()


class ExportCanceled(Exception):
    """Raised inside a job when the user cancels it."""


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _active_listener_debt(db) -> bool:
    """True if any recently-active listener lacks current+next-3 cached audio.

    The existing after-play prefetch chain fills these files; the export
    merely stays off the worker until they exist.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=HEARTBEAT_WINDOW)
    recent = db.query(Progress).filter(Progress.chapter_id.isnot(None)).all()
    for prog in recent:
        if prog.updated_at is None or _as_utc(prog.updated_at) < cutoff:
            continue
        current = db.query(Chapter).filter(Chapter.id == prog.chapter_id).first()
        if current is None:
            continue
        needed = [current.id] + [
            c.id for c in db.query(Chapter)
            .filter(Chapter.novel_id == current.novel_id, Chapter.order > current.order)
            .order_by(Chapter.order).limit(PREFETCH_DEPTH).all()
        ]
        if any(not tts.temp_path_for_chapter(cid).exists() for cid in needed):
            return True
    return False


def _export_may_proceed(db) -> bool:
    """Export priority gate: everything else outranks exports."""
    return (not tts.interactive_busy()
            and not _active_listener_debt(db)
            and not library_sync.is_running())


async def _wait_for_export_turn(job_id: int):
    """Block until the export may use the TTS worker; raise on cancel."""
    waited = False
    while True:
        if job_id in _cancel_requested:
            raise ExportCanceled()
        db = SessionLocal()
        try:
            ok = _export_may_proceed(db)
        finally:
            db.close()
        if ok:
            return
        if not waited:
            _update_job(job_id, detail="waiting for playback to idle")
            waited = True
        await asyncio.sleep(2)


def _update_job(job_id: int, **fields):
    """Short-lived-session job row update (worker holds no long sessions)."""
    from database import ExportJob
    db = SessionLocal()
    try:
        job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
        if job:
            for k, v in fields.items():
                setattr(job, k, v)
            db.commit()
    finally:
        db.close()
