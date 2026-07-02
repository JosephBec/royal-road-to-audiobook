"""
Novel API routes.

Handles adding, listing, deleting novels and refreshing chapter lists.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, Novel, Chapter, Progress, Settings, effective_settings
from scrapers import get_scraper_for_url, supported_sites

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/novels", tags=["novels"])


class AddNovelRequest(BaseModel):
    url: str


class NovelSettingsRequest(BaseModel):
    voice: str | None = None
    speed: float | None = None
    auto_play: bool | None = None
    chapter_sort: str | None = None


class NovelResponse(BaseModel):
    id: int
    title: str
    author: str
    rr_url: str
    cover_url: str | None
    description: str | None
    total_chapters: int
    last_refreshed: str | None
    created_at: str
    progress_chapter: int | None = None
    progress_chapter_title: str | None = None
    settings: dict | None = None
    effective_settings: dict | None = None

    class Config:
        from_attributes = True


def _novel_settings_payload(novel: Novel, db: Session) -> dict:
    """Override values plus their resolution against global settings."""
    settings = db.query(Settings).first()
    return {
        "settings": {
            "voice": novel.voice,
            "speed": novel.speed,
            "auto_play": novel.auto_play,
            "chapter_sort": novel.chapter_sort,
        },
        "effective_settings": effective_settings(novel, settings),
    }


@router.get("", response_model=list[NovelResponse])
async def list_novels(db: Session = Depends(get_db)):
    """List all novels in the library."""
    novels = db.query(Novel).order_by(Novel.created_at.desc()).all()
    results = []
    for novel in novels:
        progress = db.query(Progress).filter(Progress.novel_id == novel.id).first()
        chapter_title = None
        chapter_order = None
        if progress and progress.chapter_id:
            ch = db.query(Chapter).filter(Chapter.id == progress.chapter_id).first()
            if ch:
                chapter_title = ch.title
                chapter_order = ch.order
        results.append(NovelResponse(
            id=novel.id,
            title=novel.title,
            author=novel.author,
            rr_url=novel.rr_url,
            cover_url=novel.cover_url,
            description=novel.description,
            total_chapters=novel.total_chapters,
            last_refreshed=novel.last_refreshed.isoformat() if novel.last_refreshed else None,
            created_at=novel.created_at.isoformat() if novel.created_at else "",
            progress_chapter=chapter_order,
            progress_chapter_title=chapter_title,
            **_novel_settings_payload(novel, db),
        ))
    return results


@router.post("", response_model=NovelResponse, status_code=201)
async def add_novel(req: AddNovelRequest, db: Session = Depends(get_db)):
    """Add a novel by Royal Road URL."""
    # Check if already exists
    existing = db.query(Novel).filter(Novel.rr_url.contains("/fiction/")).filter(
        Novel.rr_url == req.url.rstrip("/")
    ).first()

    scraper = get_scraper_for_url(req.url)
    if not scraper:
        raise HTTPException(
            status_code=400,
            detail=f"No scraper supports this URL. Supported sites: {', '.join(supported_sites())}",
        )

    try:
        metadata = await scraper.scrape_novel_metadata(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Failed to scrape novel: %s", e)
        raise HTTPException(status_code=502, detail=f"Failed to scrape {scraper.name}: {e}")

    existing = db.query(Novel).filter(Novel.rr_url == metadata["rr_url"]).first()
    if existing:
        raise HTTPException(status_code=409, detail="Novel already in library")

    # Create novel
    novel = Novel(
        title=metadata["title"],
        author=metadata["author"],
        rr_url=metadata["rr_url"],
        cover_url=metadata["cover_url"],
        description=metadata["description"],
    )
    db.add(novel)
    db.flush()

    # Scrape chapters
    try:
        chapter_list = await scraper.scrape_chapter_list(metadata["rr_url"])
    except Exception as e:
        logger.error("Failed to scrape chapters: %s", e)
        chapter_list = []

    for ch_data in chapter_list:
        chapter = Chapter(
            novel_id=novel.id,
            rr_chapter_id=ch_data["rr_chapter_id"],
            title=ch_data["title"],
            order=ch_data["order"],
            rr_url=ch_data["rr_url"],
            published_at=ch_data.get("published_at"),
        )
        db.add(chapter)

    novel.total_chapters = len(chapter_list)
    novel.last_refreshed = datetime.now(timezone.utc)
    db.commit()
    db.refresh(novel)

    logger.info("Added novel: %s (%d chapters)", novel.title, novel.total_chapters)

    return NovelResponse(
        id=novel.id,
        title=novel.title,
        author=novel.author,
        rr_url=novel.rr_url,
        cover_url=novel.cover_url,
        description=novel.description,
        total_chapters=novel.total_chapters,
        last_refreshed=novel.last_refreshed.isoformat() if novel.last_refreshed else None,
        created_at=novel.created_at.isoformat() if novel.created_at else "",
        progress_chapter=None,
        progress_chapter_title=None,
        **_novel_settings_payload(novel, db),
    )


@router.patch("/{novel_id}/settings")
async def update_novel_settings(
    novel_id: int,
    req: NovelSettingsRequest,
    db: Session = Depends(get_db),
):
    """Set or clear per-novel overrides. Explicit null clears (inherits global)."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    provided = req.model_fields_set
    if "speed" in provided and req.speed is not None and not (0.5 <= req.speed <= 2.0):
        raise HTTPException(status_code=400, detail="Speed must be between 0.5 and 2.0")
    if "chapter_sort" in provided and req.chapter_sort not in (None, "asc", "desc"):
        raise HTTPException(status_code=400, detail="Chapter sort must be 'asc' or 'desc'")

    for field in ("voice", "speed", "auto_play", "chapter_sort"):
        if field in provided:
            setattr(novel, field, getattr(req, field))
    db.commit()

    logger.info("Novel %d settings updated: %s", novel_id,
                {f: getattr(novel, f) for f in ("voice", "speed", "auto_play", "chapter_sort")})
    return _novel_settings_payload(novel, db)


@router.delete("/{novel_id}", status_code=204)
async def delete_novel(novel_id: int, db: Session = Depends(get_db)):
    """Remove a novel from the library."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    db.delete(novel)
    db.commit()


@router.post("/{novel_id}/refresh")
async def refresh_novel(novel_id: int, db: Session = Depends(get_db)):
    """Re-crawl Royal Road for new chapters."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    scraper = get_scraper_for_url(novel.rr_url)
    if not scraper:
        raise HTTPException(
            status_code=400,
            detail=f"No scraper supports this novel's URL. Supported sites: {', '.join(supported_sites())}",
        )

    try:
        chapter_list = await scraper.scrape_chapter_list(novel.rr_url)
    except Exception as e:
        logger.error("Failed to refresh chapters: %s", e)
        raise HTTPException(status_code=502, detail=f"Failed to scrape: {e}")

    existing_urls = {ch.rr_url for ch in novel.chapters}
    new_count = 0

    for ch_data in chapter_list:
        if ch_data["rr_url"] not in existing_urls:
            chapter = Chapter(
                novel_id=novel.id,
                rr_chapter_id=ch_data["rr_chapter_id"],
                title=ch_data["title"],
                order=ch_data["order"],
                rr_url=ch_data["rr_url"],
                published_at=ch_data.get("published_at"),
            )
            db.add(chapter)
            new_count += 1

    novel.total_chapters = len(chapter_list)
    novel.last_refreshed = datetime.now(timezone.utc)
    db.commit()

    logger.info("Refreshed %s: %d new chapters", novel.title, new_count)
    return {"new_chapters": new_count, "total_chapters": novel.total_chapters}
