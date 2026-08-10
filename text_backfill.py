"""
Backfilling chapter text.

Text is only cached as a side effect of playing or prefetching a chapter, so
anything behind your current position was never fetched. That blocks the
things that read across a whole book — pronunciation scanning, speaker
tagging, searching — none of which should require listening first.

Text is cheap: about 20 KB a chapter, so an entire 600-chapter novel is around
12 MB. Audio is not, which is why only text is backfilled here.

The cost is on someone else's server, so requests are spaced out and a job
touches one novel at a time. EPUBs read from the local file and skip the
delay entirely.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from database import SessionLocal, Chapter, Novel
from scrapers import get_scraper_for_url

logger = logging.getLogger(__name__)

# Space out requests to the source site. Nothing here is urgent, and a
# backfill can be hundreds of chapters.
DELAY_SECONDS = 1.2
EPUB_DELAY_SECONDS = 0.0   # reads a local file; no one to be polite to

_task: asyncio.Task | None = None
_state: dict = {"running": False}


def status() -> dict:
    return dict(_state)


def is_running() -> bool:
    return _task is not None and not _task.done()


def cancel():
    if is_running():
        _task.cancel()
        _state["running"] = False
        _state["detail"] = "cancelled"


def start(novel_id: int, start_order: int | None, end_order: int | None) -> dict:
    """Begin a backfill unless one is already going."""
    global _task
    if is_running():
        return {"started": False, "reason": "a backfill is already running",
                "status": status()}
    _task = asyncio.create_task(_run(novel_id, start_order, end_order))
    return {"started": True, "status": status()}


def _pending_chapters(db, novel_id, start_order, end_order):
    query = db.query(Chapter).filter(Chapter.novel_id == novel_id,
                                     Chapter.text.is_(None))
    if start_order is not None:
        query = query.filter(Chapter.order >= start_order)
    if end_order is not None:
        query = query.filter(Chapter.order <= end_order)
    return query.order_by(Chapter.order).all()


async def _run(novel_id: int, start_order: int | None, end_order: int | None):
    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        if novel is None:
            return
        title, novel_url = novel.title, novel.rr_url
        targets = [(c.id, c.rr_url) for c in
                   _pending_chapters(db, novel_id, start_order, end_order)]
    finally:
        db.close()

    is_epub = (novel_url or "").startswith("epub://")
    delay = EPUB_DELAY_SECONDS if is_epub else DELAY_SECONDS

    _state.update({"running": True, "novel_id": novel_id, "novel": title,
                   "total": len(targets), "done": 0, "failed": 0,
                   "detail": "starting"})
    logger.info("Text backfill for '%s': %d chapter(s) missing text",
                title, len(targets))

    scraper = get_scraper_for_url(novel_url)
    if scraper is None:
        _state.update({"running": False, "detail": "no scraper for this novel"})
        return

    try:
        for index, (chapter_id, url) in enumerate(targets, start=1):
            _state["detail"] = f"fetching {index} of {len(targets)}"
            try:
                text = await scraper.scrape_chapter_text(url)
            except Exception as e:
                _state["failed"] += 1
                logger.warning("Backfill failed for chapter %d: %s", chapter_id, e)
                # A missing chapter shouldn't abandon the rest of the book.
                await asyncio.sleep(delay)
                continue

            db = SessionLocal()
            try:
                chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
                if chapter is not None and not chapter.text:
                    chapter.text = text
                    chapter.word_count = len(text.split())
                    chapter.fetched_at = datetime.now(timezone.utc)
                    db.commit()
            finally:
                db.close()

            _state["done"] += 1
            if delay:
                await asyncio.sleep(delay)
    except asyncio.CancelledError:
        _state.update({"running": False, "detail": "cancelled"})
        raise
    finally:
        if _state.get("detail") != "cancelled":
            _state.update({"running": False, "detail": "finished"})
            logger.info("Text backfill for '%s' finished: %d fetched, %d failed",
                        title, _state["done"], _state["failed"])
