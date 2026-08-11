"""
Background favorites pipeline.

Triggered from the frontend on every page load (cooldown-limited): re-crawls
each favorite novel for new chapters, applies retention, and asks the cache
sweep to re-plan — a new release of a favourite you are caught up with is the
one thing that outranks everything else on the GPU (see cache_policy).

Finding chapters and rendering them are deliberately separate jobs here. This
one only discovers.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import cache_policy
from database import SessionLocal, Novel, Chapter, ensure_progress
from scrapers import get_scraper_for_url
import prefetch
import tts

logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 600  # at most one full sync per 10 minutes

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
    """Insert newly scraped chapters; returns how many were new.

    Identity is the site's own chapter id, NOT the URL. Royal Road URLs embed
    the fiction slug, and authors rename fictions (e.g. adding a stubbing
    notice), which rewrites every chapter URL at once. Keying on the URL made
    that look like a few hundred brand-new chapters and re-imported the whole
    back catalogue — doubling the library and inflating unread counts. When a
    known chapter shows up under a new URL we just update the stored one.
    """
    by_chapter_id = {ch.rr_chapter_id: ch for ch in novel.chapters if ch.rr_chapter_id}
    existing_urls = {ch.rr_url for ch in novel.chapters}
    # New chapters are appended after the current max order, NOT given the
    # crawl's positional order. After a stub the crawl re-indexes from 1, so
    # trusting ch_data["order"] would collide new chapters with existing ones
    # (two chapters sharing an order breaks next/prev and play ordering).
    next_order = max((ch.order for ch in novel.chapters), default=0) + 1
    new_count = 0
    renamed = 0
    for ch_data in chapter_list:
        site_id = ch_data.get("rr_chapter_id")
        url = ch_data["rr_url"]
        known = by_chapter_id.get(site_id) if site_id else None

        if known is not None:
            # Same chapter, moved URL (slug rename). Adopt the new URL unless
            # some other row already holds it, which would break the
            # (novel_id, rr_url) unique constraint.
            if known.rr_url != url and url not in existing_urls:
                existing_urls.discard(known.rr_url)
                known.rr_url = url
                existing_urls.add(url)
                renamed += 1
            continue

        if url in existing_urls:
            continue  # no site id to match on, but we already have this URL

        chapter = Chapter(
            novel_id=novel.id,
            rr_chapter_id=site_id,
            title=ch_data["title"],
            order=next_order,
            chapter_number=ch_data.get("chapter_number"),
            rr_url=url,
            published_at=ch_data.get("published_at"),
        )
        db.add(chapter)
        existing_urls.add(url)
        if site_id:
            by_chapter_id[site_id] = chapter
        next_order += 1
        new_count += 1

    if renamed:
        logger.info("%s: %d chapter URL(s) updated after a slug change", novel.title, renamed)
    # Count what we actually have, not the crawl length. Authors "stub" novels
    # (pull chapters for Amazon exclusivity), so a later crawl can be shorter
    # than the library. We never delete stored chapters, and the count must
    # never drop below them.
    novel.total_chapters = len(existing_urls)
    novel.last_refreshed = datetime.now(timezone.utc)
    # A novel imported while the site was down has no chapters and so no
    # current chapter; the first successful crawl is when it can finally get
    # one. No-op for every novel that already has one.
    ensure_progress(db, novel)
    db.commit()
    return new_count


async def _run():
    logger.info("Favorites sync started")
    db = SessionLocal()
    try:
        favorite_ids = [n.id for n in db.query(Novel).filter(
            Novel.favorite.is_(True), Novel.archived.is_(False)).all()]
    finally:
        db.close()

    for novel_id in favorite_ids:
        try:
            await _sync_novel(novel_id)
        except Exception:
            logger.exception("Favorites sync failed for novel %d", novel_id)

    db = SessionLocal()
    try:
        keep, expiring = cache_policy.retention_sets(db)
    finally:
        db.close()
    tts.cleanup_temp_files(keep, expiring)
    logger.info("Favorites sync complete (%d favorites)", len(favorite_ids))

    # New chapters may have landed, which is the one event that can put a
    # favourite's new release at the top of the plan. Ask for a sweep now
    # rather than waiting out the idle tick.
    prefetch.request_sweep()

    # Cache any chapter text that is still missing. Chapter text is only stored
    # as a side effect of playing or prefetching, so everything behind the
    # reader's position was never fetched — and pronunciation scanning, speaker
    # tagging and search all read across the whole book. A library's worth is
    # roughly 12 MB, so there is nothing to weigh up; it just needs doing, and
    # this is where new chapters are already being discovered.
    try:
        import text_backfill
        await text_backfill.backfill_all_active()
    except Exception:
        logger.exception("Background text backfill failed")


async def _sync_novel(novel_id: int):
    """Crawl one favourite for new chapters. Rendering is not this job.

    This used to pick the next three chapters and queue them itself, which
    meant the crawl held an opinion about caching that could disagree with the
    sweep's. It only has to find the chapters; cache_policy decides what any of
    that is worth rendering.
    """
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
        try:
            chapter_list = await scraper.scrape_chapter_list(novel.rr_url)
            new_count = sync_chapter_list(db, novel, chapter_list)
            if new_count:
                logger.info("Favorite %s: %d new chapter(s)", title, new_count)
        except Exception as e:
            logger.warning("Chapter refresh failed for favorite %s: %s", title, e)
    finally:
        db.close()
