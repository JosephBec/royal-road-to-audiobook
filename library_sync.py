"""
Background favorites pipeline.

Triggered from the frontend on every page load (cooldown-limited): re-crawls
each favorite novel for new chapters, then pre-downloads the next 3 chapters
from each favorite's saved progress so new releases are always ready to play.
Yields to any synthesis the user is actively waiting on.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from database import (
    SessionLocal, Novel, Chapter, Progress, Settings,
    effective_settings, retention_policy,
)
from scrapers import get_scraper_for_url
import prefetch
import tts

logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 600  # at most one full sync per 10 minutes
PREFETCH_DEPTH = 3

_last_run = 0.0
_task: asyncio.Task | None = None


def start_refresh() -> dict:
    """Kick off a background sync unless one ran recently or is running."""
    global _last_run, _task
    now = time.time()
    if _task and not _task.done():
        return {"started": False, "reason": "already running", "cooldown_remaining": 0}
    remaining = COOLDOWN_SECONDS - (now - _last_run)
    if remaining > 0:
        return {"started": False, "reason": "cooldown", "cooldown_remaining": int(remaining)}
    _last_run = now
    _task = asyncio.create_task(_run())
    return {"started": True, "cooldown_remaining": COOLDOWN_SECONDS}


def is_running() -> bool:
    """True while a favorites sync pass is active (exports yield to it)."""
    return _task is not None and not _task.done()


def sync_chapter_list(db, novel: Novel, chapter_list: list[dict]) -> int:
    """Insert newly scraped chapters; returns how many were new."""
    existing_urls = {ch.rr_url for ch in novel.chapters}
    new_count = 0
    for ch_data in chapter_list:
        if ch_data["rr_url"] not in existing_urls:
            db.add(Chapter(
                novel_id=novel.id,
                rr_chapter_id=ch_data["rr_chapter_id"],
                title=ch_data["title"],
                order=ch_data["order"],
                rr_url=ch_data["rr_url"],
                published_at=ch_data.get("published_at"),
            ))
            existing_urls.add(ch_data["rr_url"])
            new_count += 1
    # Count what we actually have, not the crawl length. Authors "stub" novels
    # (pull chapters for Amazon exclusivity), so a later crawl can be shorter
    # than the library. We never delete stored chapters, and the count must
    # never drop below them.
    novel.total_chapters = len(existing_urls)
    novel.last_refreshed = datetime.now(timezone.utc)
    db.commit()
    return new_count


async def _run():
    logger.info("Favorites sync started")
    db = SessionLocal()
    try:
        favorite_ids = [n.id for n in db.query(Novel).filter(Novel.favorite.is_(True)).all()]
    finally:
        db.close()

    for novel_id in favorite_ids:
        try:
            await _sync_novel(novel_id)
        except Exception:
            logger.exception("Favorites sync failed for novel %d", novel_id)

    db = SessionLocal()
    try:
        forever, expiring = retention_policy(db)
    finally:
        db.close()
    tts.cleanup_temp_files(forever, expiring)
    logger.info("Favorites sync complete (%d favorites)", len(favorite_ids))


async def _sync_novel(novel_id: int):
    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        if not novel:
            return
        title = novel.title
        scraper = get_scraper_for_url(novel.rr_url)
        if not scraper:
            logger.warning("No scraper for favorite %s, skipping", title)
            return

        # 1. Look for new chapters
        try:
            chapter_list = await scraper.scrape_chapter_list(novel.rr_url)
            new_count = sync_chapter_list(db, novel, chapter_list)
            if new_count:
                logger.info("Favorite %s: %d new chapter(s)", title, new_count)
        except Exception as e:
            logger.warning("Chapter refresh failed for favorite %s: %s", title, e)

        # 2. Determine the next chapters from saved progress
        settings = db.query(Settings).first()
        voice = effective_settings(novel, settings)["voice"]
        current_order = 0
        prog = db.query(Progress).filter(Progress.novel_id == novel.id).first()
        if prog and prog.chapter_id:
            ch = db.query(Chapter).filter(Chapter.id == prog.chapter_id).first()
            if ch:
                current_order = ch.order
        targets = (
            db.query(Chapter)
            .filter(Chapter.novel_id == novel.id, Chapter.order > current_order)
            .order_by(Chapter.order)
            .limit(PREFETCH_DEPTH)
            .all()
        )
        target_data = [(c.id, c.rr_url, c.title) for c in targets]
    finally:
        db.close()

    # 3. Hand render-ahead to the single prefetch worker (dedups against
    #    playback-triggered prefetch so nothing is synthesized twice).
    prefetch.enqueue(target_data, voice)
