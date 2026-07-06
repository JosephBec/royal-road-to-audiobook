"""
EPUBs folder sync service.

The EPUBs folder is the source of truth for locally-added books: files that
appear become library novels, files that disappear take their novel (and
progress) with them, and a file replaced under the same name re-syncs its
chapter list while keeping progress.

Runs as an asyncio loop started from the app lifespan. A new file must hold
a stable size/mtime across two consecutive polls before registration so a
file still being copied in is never parsed. A file whose parse fails is
skipped and retried only when it changes on disk.
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from database import SessionLocal, Novel, Chapter
from library_sync import sync_chapter_list
from scrapers import epub_local
from tts import remove_chapter_audio

logger = logging.getLogger(__name__)

POLL_SECONDS = 5.0

_task: asyncio.Task | None = None
_seen: dict[str, tuple[int, int]] = {}      # filename -> (mtime_ns, size) last handled
_pending: dict[str, tuple[int, int]] = {}   # new files awaiting a stable signature
# asyncio.Lock binds to the loop that first acquires it; tests run many
# short-lived loops, so keep one lock per event loop.
_locks: dict[int, asyncio.Lock] = {}


def _lock() -> asyncio.Lock:
    return _locks.setdefault(id(asyncio.get_running_loop()), asyncio.Lock())


def start():
    """Create folders and start the polling loop (called from app lifespan)."""
    global _task
    epub_local.EPUB_DIR.mkdir(exist_ok=True)
    epub_local.covers_dir().mkdir(exist_ok=True)
    _task = asyncio.create_task(_loop())
    logger.info("EPUB folder sync watching %s", epub_local.EPUB_DIR)


def stop():
    if _task and not _task.done():
        _task.cancel()


def reset():
    """Clear in-memory state (tests only)."""
    _seen.clear()
    _pending.clear()
    _locks.clear()


def forget(filename: str):
    """Drop tracking for a file the app itself deleted (UI delete)."""
    _seen.pop(filename, None)
    _pending.pop(filename, None)


async def _loop():
    while True:
        try:
            await sync_once()
        except Exception:
            logger.exception("EPUB folder sync pass failed")
        await asyncio.sleep(POLL_SECONDS)


async def sync_now(filename: str):
    """Register a just-uploaded file immediately — the upload endpoint only
    calls this after the file is fully written, so no stability wait."""
    path = epub_local.EPUB_DIR / filename
    st = path.stat()
    _pending[filename] = (st.st_mtime_ns, st.st_size)
    await sync_once()


async def sync_once():
    """One diff pass: folder contents vs epub:// novels in the database."""
    async with _lock():
        db = SessionLocal()
        try:
            files = {p.name: p.stat() for p in epub_local.EPUB_DIR.glob("*.epub")}
            novels = {
                epub_local.filename_from_url(n.rr_url): n
                for n in db.query(Novel).filter(Novel.rr_url.like("epub://%")).all()
            }

            for name in set(novels) - set(files):
                _remove(db, novels[name], name)
            for name in set(_seen) - set(files):
                _seen.pop(name, None)
            for name in set(_pending) - set(files):
                _pending.pop(name, None)

            for name, st in files.items():
                sig = (st.st_mtime_ns, st.st_size)
                if _seen.get(name) == sig:
                    continue  # unchanged since last handled (or known-bad)
                novel = novels.get(name)
                if novel is not None:
                    if name in _seen:  # replaced/extended edition
                        await _resync(db, novel, name)
                    _seen[name] = sig  # startup snapshot, or record the resync
                    continue
                if _pending.get(name) != sig:
                    _pending[name] = sig  # first sighting; wait for stability
                    continue
                del _pending[name]
                await _register(db, name)
                _seen[name] = sig  # success or parse failure: retry only on change
        finally:
            db.close()


async def _register(db, filename: str):
    path = epub_local.EPUB_DIR / filename
    try:
        parsed = await asyncio.to_thread(epub_local.parse_epub_file, path)
    except Exception:
        logger.exception("Cannot parse %s — skipping until the file changes", filename)
        return
    novel = Novel(
        title=parsed.title,
        author=parsed.author,
        rr_url=epub_local.novel_url(filename),
        description=parsed.description,
    )
    db.add(novel)
    db.flush()
    if parsed.cover:
        epub_local.covers_dir().mkdir(exist_ok=True)
        cover_path = epub_local.covers_dir() / f"{Path(filename).stem}.{parsed.cover_ext}"
        cover_path.write_bytes(parsed.cover)
        novel.cover_url = f"/api/epubs/{novel.id}/cover"
    for ch in parsed.chapters:
        db.add(Chapter(
            novel_id=novel.id,
            rr_chapter_id=str(ch.index),
            title=ch.title,
            order=ch.index + 1,
            rr_url=epub_local.chapter_url(filename, ch.index),
            word_count=ch.word_count,
        ))
    novel.total_chapters = len(parsed.chapters)
    novel.last_refreshed = datetime.now(timezone.utc)
    db.commit()
    logger.info("Registered EPUB: %s (%d chapters)", parsed.title, novel.total_chapters)


async def _resync(db, novel: Novel, filename: str):
    path = epub_local.EPUB_DIR / filename
    try:
        parsed = await asyncio.to_thread(epub_local.parse_epub_file, path)
    except Exception:
        logger.exception("Cannot re-parse %s — keeping existing chapters", filename)
        return
    chapter_list = [{
        "title": ch.title,
        "rr_url": epub_local.chapter_url(filename, ch.index),
        "rr_chapter_id": str(ch.index),
        "order": ch.index + 1,
        "published_at": None,
    } for ch in parsed.chapters]
    new_count = sync_chapter_list(db, novel, chapter_list)
    if new_count:
        logger.info("EPUB %s: %d new chapter(s)", filename, new_count)


def _remove(db, novel: Novel, filename: str):
    title = novel.title
    remove_chapter_audio({ch.id for ch in novel.chapters})
    for cover in epub_local.covers_dir().glob(f"{Path(filename).stem}.*"):
        cover.unlink(missing_ok=True)
    db.delete(novel)
    db.commit()
    forget(filename)
    logger.info("EPUB file removed — deleted novel: %s", title)
