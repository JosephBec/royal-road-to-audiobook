"""The cache sweep: one worker that keeps the reading window's openings warm.

Given a plan from cache_policy — every chapter that should be holding its
first two minutes, most urgent first — this renders whatever is missing, in
order, and then runs retention. Playback, the favourites crawl and novel
imports do not hand it work; they only tell it something changed.

That is the difference from the queue this replaces. A queue of chapters is
wrong twice over: it is stale the moment you jump to a different chapter, and
it dies with the process, so a restart mid-render lost the ordering as well as
the audio. The plan is a function of the database, so it is always current and
a restart costs nothing.

The plan is rebuilt between every chapter rather than once per pass. It is a
handful of indexed queries against a small database, which is nothing next to
the two minutes of GPU it is deciding how to spend — and it means pressing play
on something new re-prioritises immediately instead of at the end of the pass.
"""

import asyncio
import logging
from datetime import datetime, timezone

import cache_policy
from cache_policy import HEAD_START_SECONDS
from database import SessionLocal, Chapter
from scrapers import get_scraper_for_url
import text_rules
import tts

logger = logging.getLogger(__name__)

# How long the worker waits for a nudge before sweeping anyway. Nothing signals
# a quiet server, and idle time is exactly when this work should be happening.
IDLE_TICK_SECONDS = 120

_wake: asyncio.Event | None = None
_worker_task: asyncio.Task | None = None
_sweeping = False               # a pass is underway, including between renders
_deferred: set[int] = set()     # failed this pass; retried on the next one


def _ensure_wake() -> asyncio.Event:
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    return _wake


def reset():
    """Test hook: fresh state, no background task."""
    global _wake, _sweeping
    _wake = asyncio.Event()
    _sweeping = False
    _deferred.clear()


def start_worker():
    """Launch the sweep worker with an event bound to the current loop.

    Always a new event, never one that may belong to a closed loop (e.g. across
    TestClient app instances).
    """
    global _wake, _worker_task, _sweeping
    _wake = asyncio.Event()
    _sweeping = False
    _deferred.clear()
    # Sweep straight away rather than waiting out the first idle tick. A
    # restart is the moment the cache is most likely to be behind — anything
    # interrupted mid-render is still short of its opening — and two minutes of
    # doing nothing is two minutes the next press of play may have to pay for.
    _wake.set()
    _worker_task = asyncio.create_task(_worker_loop())
    logger.info("Cache sweep worker started")


def stop():
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()


def request_sweep():
    """Tell the worker the plan may have changed — progress moved, chapters
    arrived, a novel was imported.

    Carries no payload on purpose. The caller knowing *what* changed is exactly
    the coupling that made the old queue drift out of step with reality; the
    worker re-reads the database and works it out.
    """
    _deferred.clear()
    if _wake is not None:
        _wake.set()


def is_busy() -> bool:
    """True while a sweep is running or pending (export gate).

    The whole pass counts, not just the moments a render is in flight — the
    planner's queries between renders are short, but an export that slipped
    into one would hold the TTS worker for a full batch before yielding.
    """
    return _sweeping or (_wake is not None and _wake.is_set())


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

    Mirrors routers.chapters.get_chapter_text so the sweep honors the same
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
            logger.warning("No scraper for sweep target %s", url)
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


def _next_target() -> cache_policy.CacheTarget | None:
    """The most urgent chapter still missing its opening."""
    db = SessionLocal()
    try:
        for target in cache_policy.cache_plan(db):
            if target.chapter_id in _deferred:
                continue
            if cache_policy.head_start_satisfied(target.chapter_id):
                continue
            return target
        return None
    finally:
        db.close()


async def _render_head_start(target: cache_policy.CacheTarget) -> bool:
    """Render this chapter's opening. True when it is genuinely satisfied after.

    The return value is the loop's termination guarantee, not a status report.
    The sweep picks targets by "does this chapter have its opening yet", so a
    render that ends without producing one — a scrape failure, a synthesis
    error — would be chosen again immediately and forever. Confirming the
    outcome against the same condition the planner uses is what makes that
    impossible.
    """
    try:
        await _wait_for_interactive_idle()
        text = await _fetch_text(target.chapter_id, target.url)
        if text is None:
            return False
        body = _apply_rules(target.chapter_id, text)
        await tts.synthesize_chapter_streaming(
            target.chapter_id, f"{target.title}\n\n{body}",
            target.voice, 1.0, target.engine,
            None, HEAD_START_SECONDS, yield_to_interactive=True)
        return cache_policy.head_start_satisfied(target.chapter_id)
    except Exception:
        logger.exception("Head start failed for chapter %d (%s)",
                         target.chapter_id, target.title)
        return False


def _run_retention_cleanup():
    db = SessionLocal()
    try:
        keep, expiring = cache_policy.retention_sets(db)
    finally:
        db.close()
    # cleanup_temp_files itself protects chapters mid-render.
    tts.cleanup_temp_files(keep, expiring)


async def sweep_once() -> int:
    """Render every missing opening in priority order, then run retention.

    Both the worker's unit of work and the test seam.
    """
    global _sweeping
    rendered = 0
    _sweeping = True
    try:
        while True:
            target = _next_target()
            if target is None:
                break
            logger.info("Head start: chapter %d (%s) — %s",
                        target.chapter_id, target.title, target.reason)
            if await _render_head_start(target):
                rendered += 1
            else:
                _deferred.add(target.chapter_id)
        _run_retention_cleanup()
    finally:
        _sweeping = False
    return rendered


async def _worker_loop():
    wake = _ensure_wake()
    while True:
        try:
            await asyncio.wait_for(wake.wait(), timeout=IDLE_TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
        wake.clear()
        # Anything that failed last pass gets one more chance. Failures here are
        # usually a scrape that timed out, which the next attempt often wins.
        _deferred.clear()
        try:
            await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Cache sweep failed")
