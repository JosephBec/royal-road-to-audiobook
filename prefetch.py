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
import text_rules
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


def enqueue(targets: list[tuple[int, str, str]], voice: str, engine: str | None = None):
    """Queue chapters to render ahead. `targets` are (chapter_id, url, title).

    Skips chapters already rendered on disk or already queued/in-flight, so
    repeated triggers for overlapping chapters do no duplicate work.

    The engine is captured per item rather than read at render time: a queued
    chapter should render with the engine that was in effect when it was
    queued, not whatever the user has switched to by the time it drains.
    """
    queue = _ensure_queue()
    for chapter_id, url, title in targets:
        if chapter_id in _pending or chapter_id in _inflight:
            continue
        if tts.temp_path_for_chapter(chapter_id).exists():
            continue
        _pending.add(chapter_id)
        queue.put_nowait((chapter_id, url, title, voice, engine))


async def _wait_for_interactive_idle():
    """Yield the TTS worker while the user is waiting on a chapter."""
    while tts.interactive_busy():
        await asyncio.sleep(2)


def _apply_rules(chapter_id: int, text: str) -> str:
    """Rules are applied at render time, not scrape time, so changing one does
    not require re-fetching and a bad rule can never corrupt the cached text."""
    db = SessionLocal()
    try:
        chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
        novel_id = chapter.novel_id if chapter else None
        return text_rules.speech_text(db, novel_id, text)
    finally:
        db.close()


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


async def _process_one(item: tuple):
    # Tolerate the 4-tuple form so an item queued before an upgrade still drains.
    chapter_id, url, title, voice = item[:4]
    engine = item[4] if len(item) > 4 else None
    _inflight.add(chapter_id)
    try:
        if tts.temp_path_for_chapter(chapter_id).exists():
            return
        await _wait_for_interactive_idle()
        text = await _fetch_text(chapter_id, url)
        if text is None:
            return
        body = _apply_rules(chapter_id, text)
        # Stream rather than synthesize_chapter_to_file, which renders the
        # whole chapter into memory and writes once at the end. That is twenty
        # minutes of GPU with nothing on disk until the final instant, so a
        # restart — or a crash, or a power cut — loses all of it, and the next
        # attempt starts from zero. Render-ahead is exactly the work most
        # likely to be interrupted, since it runs unattended for hours.
        #
        # The streaming path writes each segment as it is produced and records
        # a fingerprint, so an interrupted render resumes where it stopped and
        # the segments already written stay playable. It finishes by writing
        # the same complete WAV, so nothing downstream can tell the difference.
        await tts.synthesize_chapter_streaming(
            chapter_id, f"{title}\n\n{body}", voice, 1.0, engine,
            None, None, yield_to_interactive=True)
        logger.info("Prefetched chapter %d — %s", chapter_id, title)
    except Exception:
        logger.exception("Prefetch failed for chapter %d (%s)", chapter_id, title)
    finally:
        _inflight.discard(chapter_id)
        _pending.discard(chapter_id)


# Enough audio to start listening while the rest of the chapter renders.
# Chatterbox needs ~25-60s to load plus ~10s for a first chunk, which is the
# worst moment in the whole experience; this pays that cost in advance.
HEAD_START_SECONDS = 120

# How long the worker waits for queued work before doing a head-start sweep.
IDLE_TICK_SECONDS = 120


def _head_start_satisfied(chapter_id: int) -> bool:
    """True when this chapter already holds its opening on disk.

    The idle sweep runs every couple of minutes, so it has to be free for
    chapters that are already done. Without this the render would be re-entered
    to discover it has nothing to do — and resuming deliberately drops the last
    segment as possibly-truncated, so each tick would throw away a segment and
    render it again forever.
    """
    durations = tts._read_sidecar(chapter_id).get("durations") or []
    return sum(d for d in durations if d) >= HEAD_START_SECONDS


async def head_start_pass():
    """Render the opening of each active novel's current chapter.

    Runs after the queue drains, so it never delays real prefetch. Archived
    novels are skipped — the whole point of archiving is to stop spending GPU
    time on books you aren't reading.
    """
    from database import Novel, Progress, Settings, effective_settings

    db = SessionLocal()
    try:
        settings = db.query(Settings).first()
        targets = []
        for novel in db.query(Novel).filter(Novel.archived.is_(False)).all():
            prog = db.query(Progress).filter(Progress.novel_id == novel.id).first()
            if prog and prog.chapter_id:
                chapter = db.query(Chapter).filter(Chapter.id == prog.chapter_id).first()
            else:
                # Never opened: the chapter you would press play on is the
                # first one. Skipping these meant a brand-new novel was the
                # one case guaranteed to pay the full cold-start cost.
                chapter = (db.query(Chapter)
                           .filter(Chapter.novel_id == novel.id)
                           .order_by(Chapter.order).first())
            if chapter is None or tts.temp_path_for_chapter(chapter.id).exists():
                continue  # already rendered in full; nothing to get ahead of
            if _head_start_satisfied(chapter.id):
                continue  # opening already on disk
            eff = effective_settings(novel, settings)
            targets.append((chapter.id, chapter.rr_url, chapter.title,
                            eff["voice"], eff["engine"]))
    finally:
        db.close()

    for chapter_id, url, title, voice, engine_name in targets:
        if chapter_id in _inflight:
            continue
        try:
            await _wait_for_interactive_idle()
            text = await _fetch_text(chapter_id, url)
            if text is None:
                continue
            body = _apply_rules(chapter_id, text)
            await tts.synthesize_chapter_streaming(
                chapter_id, f"{title}\n\n{body}", voice, 1.0, engine_name,
                None, HEAD_START_SECONDS, yield_to_interactive=True)
        except Exception:
            logger.exception("Head start failed for chapter %d", chapter_id)


def _run_retention_cleanup():
    db = SessionLocal()
    try:
        forever, expiring = retention_policy(db)
    finally:
        db.close()
    tts.cleanup_temp_files(forever, expiring)


async def drain_once():
    """Head start first, then everything queued, then retention cleanup once.

    This is both the worker's per-wake unit of work and the test seam.
    """
    queue = _ensure_queue()
    if queue.empty():
        return
    # The opening of what you are listening to outranks the whole of what you
    # are not. See _worker_loop.
    await head_start_pass()
    while not queue.empty():
        await _process_one(queue.get_nowait())
    _run_retention_cleanup()


async def _worker_loop():
    queue = _ensure_queue()
    while True:
        # Block for the first item, then drain the rest as a batch so cleanup
        # runs once per burst rather than per chapter.
        #
        # Waking on a timeout matters as much as waking on an item. Nothing
        # enqueues on a quiet server, so a plain get() left the worker parked
        # forever and the head start — the entire reason a chapter can start
        # instantly — never ran until something else happened to queue work.
        # Idle time is when that job should be running, not when it stops.
        try:
            first = await asyncio.wait_for(queue.get(), timeout=IDLE_TICK_SECONDS)
        except asyncio.TimeoutError:
            await head_start_pass()
            continue

        # Head start before render-ahead, not after it.
        #
        # Pressing play enqueues the next three chapters, and _process_one
        # renders each one in full — twenty minutes apiece at the ~1.4x
        # realtime Chatterbox manages, so an hour before the queue drains.
        # Running the head start at the end meant the opening two minutes of
        # the chapter actually being listened to was queued behind an hour of
        # work for chapters not yet reached. The cache filled up with the
        # wrong chapters: complete renders of ones nobody had opened, nothing
        # for the one under the playhead.
        #
        # It is cheap to put first — 120 seconds of audio per active novel,
        # skipped entirely for any chapter already on disk.
        await head_start_pass()

        await _process_one(first)
        while not queue.empty():
            await _process_one(queue.get_nowait())
        _run_retention_cleanup()
