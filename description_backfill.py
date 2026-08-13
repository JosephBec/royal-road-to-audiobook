"""
One-shot startup repair for pre-sanitizer novel descriptions.

Descriptions used to be flattened with get_text(strip=True), which glued
every text node together ("onAmazonandAudible") and dropped the links a
synopsis actually has. The fix — sanitize_description_html /
text_to_description_html in scrapers.base — only runs at scrape time, so
books added before it keep their smashed text until re-scraped.

The two formats are distinguishable: sanitized descriptions always carry
markup (<p>, <a>, ...), flattened ones never do. Each stale novel costs one
metadata re-scrape; EPUB books re-parse their local file for free. A novel
whose scrape fails keeps its old text and is retried on the next startup,
because the format check still flags it.
"""

import asyncio
import logging

from database import SessionLocal, Novel
from scrapers import get_scraper_for_url

logger = logging.getLogger(__name__)

# Space out requests to source sites, same as text_backfill: nothing here is
# urgent, and the cost lands on someone else's server.
DELAY_SECONDS = 1.2

_task: asyncio.Task | None = None


def is_stale(description: str | None) -> bool:
    """True for a non-empty description in the old flattened format."""
    return bool(description) and "<" not in description


def _stale_novels() -> list[tuple[int, str, str]]:
    db = SessionLocal()
    try:
        return [(n.id, n.rr_url, n.title) for n in db.query(Novel).all()
                if is_stale(n.description)]
    finally:
        db.close()


async def backfill_stale_descriptions():
    """Re-scrape the description of every novel still in the old format."""
    stale = _stale_novels()
    if not stale:
        return
    logger.info("Description backfill: %d novel(s) in the pre-sanitizer format",
                len(stale))
    updated = 0
    for novel_id, url, title in stale:
        scraper = get_scraper_for_url(url)
        if scraper is None:
            logger.warning("Description backfill: no scraper for %s", url)
            continue
        try:
            metadata = await scraper.scrape_novel_metadata(url)
        except Exception as e:
            logger.warning("Description backfill failed for '%s': %s", title, e)
            continue
        description = (metadata.get("description") or "").strip()
        if not description:
            continue  # never trade existing text for nothing

        db = SessionLocal()
        try:
            novel = db.query(Novel).filter(Novel.id == novel_id).first()
            # Re-check staleness: the row may have been repaired or deleted
            # while this pass was awaiting the network.
            if novel is not None and is_stale(novel.description):
                novel.description = description
                db.commit()
                updated += 1
        finally:
            db.close()

        if not url.startswith("epub://") and DELAY_SECONDS:
            await asyncio.sleep(DELAY_SECONDS)
    logger.info("Description backfill finished: %d of %d updated",
                updated, len(stale))


def start():
    """Kick off the backfill in the background (called from app lifespan)."""
    global _task
    _task = asyncio.create_task(backfill_stale_descriptions())


def stop():
    if _task and not _task.done():
        _task.cancel()
