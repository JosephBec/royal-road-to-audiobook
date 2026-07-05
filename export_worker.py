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


import shutil

import httpx
import numpy as np
import soundfile as sf

import m4b
import plex
import textbatch
from database import ExportJob, Novel, Settings
from scrapers import get_scraper_for_url

_queue: asyncio.Queue | None = None
_worker_task: asyncio.Task | None = None


def request_cancel(job_id: int):
    _cancel_requested.add(job_id)


def enqueue(job_id: int):
    assert _queue is not None, "start_worker() not called"
    _queue.put_nowait(job_id)


def startup_recover():
    """Mark jobs orphaned by a restart; re-enqueue still-queued jobs."""
    db = SessionLocal()
    try:
        for job in db.query(ExportJob).filter(ExportJob.status == "running").all():
            job.status = "interrupted"
            job.detail = "server restarted mid-export — press Retry to resume"
        db.commit()
        return [j.id for j in db.query(ExportJob).filter(ExportJob.status == "queued").all()]
    finally:
        db.close()


def start_worker():
    """Create the queue + singleton worker task; enqueue recovered jobs."""
    global _queue, _worker_task
    _queue = asyncio.Queue()
    for job_id in startup_recover():
        _queue.put_nowait(job_id)
    _worker_task = asyncio.create_task(_worker_loop())


async def _worker_loop():
    while True:
        job_id = await _queue.get()
        try:
            await _run_job(job_id)
        except Exception:
            logger.exception("Export job %d crashed", job_id)
            _update_job(job_id, status="failed", error="internal error (see server log)",
                        finished_at=datetime.now(timezone.utc))


async def _get_text(chapter_id: int) -> str:
    """Chapter text from cache, else scrape (3 attempts) and store.

    No DB session is held across the scrape awaits: read a snapshot in one
    short session, scrape with none open, store in a second short session.
    """
    db = SessionLocal()
    try:
        chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
        if chapter is None:
            raise RuntimeError(f"chapter {chapter_id} disappeared")
        cached, rr_url, title = chapter.text, chapter.rr_url, chapter.title
    finally:
        db.close()
    if cached:
        return cached

    scraper = get_scraper_for_url(rr_url)
    if scraper is None:
        raise RuntimeError(f"no scraper for {rr_url}")
    text = None
    last_err = None
    for attempt in range(3):
        try:
            text = await scraper.scrape_chapter_text(rr_url)
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
    if text is None:
        raise RuntimeError(f"failed to fetch '{title}': {last_err}")

    db = SessionLocal()
    try:
        chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
        if chapter is None:
            raise RuntimeError(f"chapter {chapter_id} disappeared")
        chapter.text = text
        chapter.word_count = len(text.split())
        chapter.fetched_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
    return text


async def _synthesize_chapter_wav(job_id, chapter_id, title, voice, speed, wav_path: Path):
    """Batch-synthesize one chapter to a PCM_16 WAV, yielding between batches."""
    text = await _get_text(chapter_id)
    text = f"{title}\n\n{text}"  # spoken chapter announcement, matches streaming
    segments: list[np.ndarray] = []
    for batch in textbatch.split_batches(text):
        await _wait_for_export_turn(job_id)
        segments.extend(await tts.synthesize_batch(batch, voice, speed))
    if not segments:
        raise RuntimeError(f"no audio produced for '{title}'")
    silence = np.zeros(int(tts.SAMPLE_RATE * 0.3), dtype=np.float32)
    parts = []
    for i, seg in enumerate(segments):
        parts.append(seg)
        if i < len(segments) - 1:
            parts.append(silence)
    sf.write(str(wav_path), np.concatenate(parts), tts.SAMPLE_RATE, subtype="PCM_16")


async def _download_cover(novel_id: int) -> tuple:
    """Best-effort cover download. Returns (bytes|None, ext)."""
    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        cover_url = novel.cover_url if novel else None
    finally:
        db.close()
    if not cover_url:
        return None, "jpg"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(cover_url)
            resp.raise_for_status()
            ext = "png" if "png" in resp.headers.get("content-type", "") else "jpg"
            return resp.content, ext
    except Exception as e:
        logger.warning("Cover download failed (continuing without): %s", e)
        return None, "jpg"


async def _run_job(job_id: int):
    _cancel_requested.discard(job_id)
    db = SessionLocal()
    try:
        job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
        if job is None or job.status == "canceled":
            return
        settings = db.query(Settings).first()
        audiobook_dir = settings.audiobook_dir if settings else ""
        plex_conf = (settings.plex_url, settings.plex_token, settings.plex_section_id) \
            if settings else ("", "", "")
        chapters = (db.query(Chapter)
                    .filter(Chapter.novel_id == job.novel_id,
                            Chapter.order >= job.start_order,
                            Chapter.order <= job.end_order)
                    .order_by(Chapter.order).all())
        plan = [(c.id, c.order, c.title) for c in chapters]
        params = (job.novel_id, job.novel_title, job.author,
                  job.start_order, job.end_order, job.voice, job.speed)
    finally:
        db.close()

    novel_id, novel_title, author, start_order, end_order, voice, speed = params
    if not plan:
        _update_job(job_id, status="failed", error="no chapters in range",
                    finished_at=datetime.now(timezone.utc))
        return
    if not audiobook_dir:
        _update_job(job_id, status="failed", error="audiobook folder not set in Settings",
                    finished_at=datetime.now(timezone.utc))
        return

    job_dir = EXPORT_DIR / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    _update_job(job_id, status="running", chapters_total=len(plan), error=None)

    try:
        done = 0
        for ch_id, order, title in plan:
            if job_id in _cancel_requested:
                raise ExportCanceled()
            wav_path = job_dir / f"chapter_{order:05d}.wav"
            if not wav_path.exists():  # retry-resume: skip finished chapters
                _update_job(job_id, detail=f"synthesizing ch. {done + 1}/{len(plan)}: {title}")
                await _synthesize_chapter_wav(job_id, ch_id, title, voice, speed, wav_path)
            done += 1
            _update_job(job_id, chapters_done=done)

        _update_job(job_id, detail="assembling M4B")
        basename = m4b.export_basename(novel_title, start_order, end_order)
        cover_bytes, cover_ext = await _download_cover(novel_id)
        chapter_wavs = [(title, job_dir / f"chapter_{order:05d}.wav")
                        for _, order, title in plan]
        m4b_path = await asyncio.to_thread(
            m4b.assemble_m4b, chapter_wavs, job_dir / f"{basename}.m4b",
            book_title=basename, author=author,
            cover_bytes=cover_bytes, cover_ext=cover_ext)

        Path(audiobook_dir).mkdir(parents=True, exist_ok=True)
        dest = Path(audiobook_dir) / m4b_path.name
        shutil.move(str(m4b_path), str(dest))

        plex_url, plex_token, plex_section = plex_conf
        if plex_url and plex_token and plex_section:
            try:
                await plex.trigger_refresh(plex_url, plex_token, plex_section)
                note = "done — Plex refresh triggered"
            except plex.PlexUnreachable:
                note = f"Audiobook saved, but {plex.PLEX_UNREACHABLE_MSG}"
            except Exception as e:
                note = f"Audiobook saved, but Plex refresh failed: {e}"
        else:
            note = "done — Plex not configured, skipped refresh"

        shutil.rmtree(job_dir, ignore_errors=True)
        _update_job(job_id, status="completed", detail=note, output_path=str(dest),
                    finished_at=datetime.now(timezone.utc))
    except ExportCanceled:
        _update_job(job_id, status="canceled", detail="canceled",
                    finished_at=datetime.now(timezone.utc))
    except Exception as e:
        logger.exception("Export job %d failed", job_id)
        _update_job(job_id, status="failed", error=str(e),
                    finished_at=datetime.now(timezone.utc))
