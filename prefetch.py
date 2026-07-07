"""
Render-ahead worker.

The single owner of audio prefetch. Both triggers — the favorites sync
(after crawling for new chapters) and playback (after a chapter is
requested) — enqueue the next chapters here instead of synthesizing inline.
One worker drains the queue serially, so a chapter is never rendered twice
and exports/playback keep priority on the GPU.

De-duplication happens on two levels: `_pending` stops the same chapter
being queued twice, and `tts.synthesize_chapter_to_file` itself serializes
any remaining concurrent request (e.g. playback rendering the same chapter
directly). When the queue drains, retention cleanup runs once.
"""

import asyncio
import logging
from datetime import datetime, timezone

from database import SessionLocal, Chapter, retention_policy
from scrapers import get_scraper_for_url
import tts

logger = logging.getLogger(__name__)

_queue: asyncio.Queue | None = None
_worker_task: asyncio.Task | None = None
_pending: set[int] = set()      # chapter ids queued but not yet finished
_inflight: set[int] = set()     # chapter ids currently being rendered


def _ensure_queue() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


def reset():
    """Test hook: fresh queue and cleared state, no background task."""
    global _queue
    _queue = asyncio.Queue()
    _pending.clear()
    _inflight.clear()


def start_worker():
    """Create a fresh queue bound to the current loop and launch the worker.

    Always makes a new queue (never reuses one that may be bound to a
    closed loop, e.g. across TestClient app instances).
    """
    global _queue, _worker_task
    _queue = asyncio.Queue()
    _pending.clear()
    _inflight.clear()
    _worker_task = asyncio.create_task(_worker_loop())
    logger.info("Prefetch worker started")


def stop():
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()


def is_busy() -> bool:
    """True while anything is queued or rendering (used by the export gate)."""
    return bool(_pending) or bool(_inflight) or (_queue is not None and not _queue.empty())


def enqueue(targets: list[tuple[int, str, str]], voice: str):
    """Queue chapters to render ahead. `targets` are (chapter_id, url, title).

    Skips chapters already rendered on disk or already queued/in-flight, so
    repeated triggers for overlapping chapters do no duplicate work.
    """
    queue = _ensure_queue()
    for chapter_id, url, title in targets:
        if chapter_id in _pending or chapter_id in _inflight:
            continue
        if tts.temp_path_for_chapter(chapter_id).exists():
            continue
        _pending.add(chapter_id)
        queue.put_nowait((chapter_id, url, title, voice))


async def _wait_for_interactive_idle():
    """Yield the TTS worker while the user is waiting on a chapter."""
    while tts.interactive_busy():
        await asyncio.sleep(2)


async def _fetch_text(chapter_id: int, url: str) -> str | None:
    """Chapter body from the DB cache, else scrape once and store it.

    Mirrors routers.chapters.get_chapter_text so prefetch honors the same
    "scrape once, ever" cache. Falls back to a plain scrape if the chapter
    row is absent.
    """
    db = SessionLocal()
    try:
        chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
        if chapter is not None and chapter.text:
            return chapter.text
        scraper = get_scraper_for_url(url)
        if not scraper:
            logger.warning("No scraper for prefetch target %s", url)
            return None
        text = await scraper.scrape_chapter_text(url)
        if chapter is not None:
            chapter.text = text
            chapter.word_count = len(text.split())
            chapter.fetched_at = datetime.now(timezone.utc)
            db.commit()
        return text
    finally:
        db.close()


async def _process_one(item: tuple[int, str, str, str]):
    chapter_id, url, title, voice = item
    _inflight.add(chapter_id)
    try:
        if tts.temp_path_for_chapter(chapter_id).exists():
            return
        await _wait_for_interactive_idle()
        text = await _fetch_text(chapter_id, url)
        if text is None:
            return
        await tts.synthesize_chapter_to_file(chapter_id, f"{title}\n\n{text}", voice, 1.0)
        logger.info("Prefetched chapter %d — %s", chapter_id, title)
    except Exception:
        logger.exception("Prefetch failed for chapter %d (%s)", chapter_id, title)
    finally:
        _inflight.discard(chapter_id)
        _pending.discard(chapter_id)


def _run_retention_cleanup():
    db = SessionLocal()
    try:
        forever, expiring = retention_policy(db)
    finally:
        db.close()
    tts.cleanup_temp_files(forever, expiring)


async def drain_once():
    """Process everything currently queued, then run retention cleanup once.

    This is both the worker's per-wake unit of work and the test seam.
    """
    queue = _ensure_queue()
    processed = False
    while not queue.empty():
        item = queue.get_nowait()
        await _process_one(item)
        processed = True
    if processed:
        _run_retention_cleanup()


async def _worker_loop():
    queue = _ensure_queue()
    while True:
        # Block for the first item, then drain the rest as a batch so cleanup
        # runs once per burst rather than per chapter.
        first = await queue.get()
        await _process_one(first)
        while not queue.empty():
            await _process_one(queue.get_nowait())
        _run_retention_cleanup()
