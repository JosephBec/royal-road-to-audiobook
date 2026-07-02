"""
Progress API routes.

Handles reading and updating playback progress for novels.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, Progress, Novel, Chapter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/progress", tags=["progress"])


class ProgressResponse(BaseModel):
    novel_id: int
    chapter_id: int | None
    chapter_order: int | None
    chapter_title: str | None
    position_seconds: float
    updated_at: str | None


class UpdateProgressRequest(BaseModel):
    chapter_id: int
    position_seconds: float = 0.0


@router.get("/{novel_id}", response_model=ProgressResponse)
async def get_progress(novel_id: int, db: Session = Depends(get_db)):
    """Get reading progress for a novel."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    progress = db.query(Progress).filter(Progress.novel_id == novel_id).first()
    if not progress:
        return ProgressResponse(
            novel_id=novel_id,
            chapter_id=None,
            chapter_order=None,
            chapter_title=None,
            position_seconds=0.0,
            updated_at=None,
        )

    chapter = db.query(Chapter).filter(Chapter.id == progress.chapter_id).first()
    return ProgressResponse(
        novel_id=novel_id,
        chapter_id=progress.chapter_id,
        chapter_order=chapter.order if chapter else None,
        chapter_title=chapter.title if chapter else None,
        position_seconds=progress.position_seconds,
        updated_at=progress.updated_at.isoformat() if progress.updated_at else None,
    )


@router.put("/{novel_id}", response_model=ProgressResponse)
async def update_progress(
    novel_id: int,
    req: UpdateProgressRequest,
    db: Session = Depends(get_db),
):
    """Update reading progress for a novel."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    chapter = db.query(Chapter).filter(Chapter.id == req.chapter_id).first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    progress = db.query(Progress).filter(Progress.novel_id == novel_id).first()
    if progress:
        progress.chapter_id = req.chapter_id
        progress.position_seconds = req.position_seconds
        progress.updated_at = datetime.now(timezone.utc)
    else:
        progress = Progress(
            novel_id=novel_id,
            chapter_id=req.chapter_id,
            position_seconds=req.position_seconds,
            updated_at=datetime.now(timezone.utc),
        )
        db.add(progress)

    db.commit()
    db.refresh(progress)

    return ProgressResponse(
        novel_id=novel_id,
        chapter_id=progress.chapter_id,
        chapter_order=chapter.order,
        chapter_title=chapter.title,
        position_seconds=progress.position_seconds,
        updated_at=progress.updated_at.isoformat() if progress.updated_at else None,
    )
