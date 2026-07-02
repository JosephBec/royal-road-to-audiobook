"""
Settings API routes.

Handles reading and updating app-wide settings (voice, speed, playback mode).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, Settings, Novel
from tts import remove_chapter_audio

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsResponse(BaseModel):
    voice: str
    speed: float
    playback_mode: str
    auto_play: bool
    theme: str
    chapter_sort: str

    class Config:
        from_attributes = True


class UpdateSettingsRequest(BaseModel):
    voice: str | None = None
    speed: float | None = None
    playback_mode: str | None = None
    auto_play: bool | None = None
    theme: str | None = None
    chapter_sort: str | None = None


@router.get("", response_model=SettingsResponse)
async def get_settings(db: Session = Depends(get_db)):
    """Get current app settings."""
    settings = db.query(Settings).first()
    if not settings:
        raise HTTPException(status_code=500, detail="Settings not initialized")
    return settings


@router.put("", response_model=SettingsResponse)
async def update_settings(req: UpdateSettingsRequest, db: Session = Depends(get_db)):
    """Update app settings."""
    settings = db.query(Settings).first()
    if not settings:
        raise HTTPException(status_code=500, detail="Settings not initialized")

    global_voice_changed = req.voice is not None and req.voice != settings.voice
    if req.voice is not None:
        settings.voice = req.voice
    if req.speed is not None:
        if not (0.5 <= req.speed <= 2.0):
            raise HTTPException(status_code=400, detail="Speed must be between 0.5 and 2.0")
        settings.speed = req.speed
    if req.playback_mode is not None:
        if req.playback_mode not in ("full", "instant"):
            raise HTTPException(status_code=400, detail="Playback mode must be 'full' or 'instant'")
        settings.playback_mode = req.playback_mode
    if req.auto_play is not None:
        settings.auto_play = req.auto_play
    if req.theme is not None:
        if req.theme not in ("dark", "light"):
            raise HTTPException(status_code=400, detail="Theme must be 'dark' or 'light'")
        settings.theme = req.theme
    if req.chapter_sort is not None:
        if req.chapter_sort not in ("asc", "desc"):
            raise HTTPException(status_code=400, detail="Chapter sort must be 'asc' or 'desc'")
        settings.chapter_sort = req.chapter_sort

    db.commit()
    db.refresh(settings)

    if global_voice_changed:
        # Invalidate cached audio for novels that inherit the global voice
        # (novels with their own voice override are unaffected)
        inheriting = db.query(Novel).filter(Novel.voice.is_(None)).all()
        ids = {ch.id for novel in inheriting for ch in novel.chapters}
        if ids:
            remove_chapter_audio(ids)

    logger.info("Settings updated: voice=%s, speed=%.1f, mode=%s, auto_play=%s",
                settings.voice, settings.speed, settings.playback_mode, settings.auto_play)
    return settings
