"""
Settings API routes.

Handles reading and updating app-wide settings (voice, speed, playback mode).
"""

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from database import get_db, Settings, Novel
from tts import remove_chapter_audio

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsResponse(BaseModel):
    engine: str
    voice: str
    speed: float
    playback_mode: str
    auto_play: bool
    theme: str
    accent: str | None
    chapter_sort: str
    audiobook_dir: str
    plex_url: str
    plex_token: str
    plex_section_id: str

    model_config = ConfigDict(from_attributes=True)


class UpdateSettingsRequest(BaseModel):
    engine: str | None = None
    voice: str | None = None
    speed: float | None = None
    playback_mode: str | None = None
    auto_play: bool | None = None
    theme: str | None = None
    accent: str | None = None
    chapter_sort: str | None = None
    audiobook_dir: str | None = None
    plex_url: str | None = None
    plex_token: str | None = None
    plex_section_id: str | None = None


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

    import engines

    global_engine_changed = req.engine is not None and req.engine != settings.engine
    if req.engine is not None:
        if req.engine not in engines.engine_names():
            raise HTTPException(status_code=400, detail=f"Unknown TTS engine '{req.engine}'")
        ok, reason = engines.get_engine(req.engine).available()
        if not ok:
            raise HTTPException(status_code=400, detail=f"Engine unavailable: {reason}")
        settings.engine = req.engine
    global_voice_changed = req.voice is not None and req.voice != settings.voice
    if req.voice is not None:
        settings.voice = req.voice
    # Switching engines strands the old voice id, so snap to something the new
    # engine actually has unless this same request already picked one.
    if global_engine_changed and req.voice is None:
        settings.voice = engines.resolve_voice(settings.engine, settings.voice)
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
        if req.theme not in ("dark", "light", "oled", "warm"):
            raise HTTPException(status_code=400,
                                detail="Theme must be one of: dark, light, oled, warm")
        settings.theme = req.theme
    if req.accent is not None:
        accent = req.accent.strip()
        if accent == "":
            settings.accent = None  # back to the theme's default accent
        elif re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
            settings.accent = accent.lower()
        else:
            raise HTTPException(status_code=400, detail="Accent must be '#rrggbb' or ''")
    if req.chapter_sort is not None:
        if req.chapter_sort not in ("asc", "desc"):
            raise HTTPException(status_code=400, detail="Chapter sort must be 'asc' or 'desc'")
        settings.chapter_sort = req.chapter_sort
    if req.audiobook_dir is not None:
        settings.audiobook_dir = req.audiobook_dir.strip()
    if req.plex_url is not None:
        settings.plex_url = req.plex_url.strip().rstrip("/")
    if req.plex_token is not None:
        settings.plex_token = req.plex_token.strip()
    if req.plex_section_id is not None:
        settings.plex_section_id = req.plex_section_id.strip()

    db.commit()
    db.refresh(settings)

    if global_voice_changed or global_engine_changed:
        # Invalidate cached audio for novels that inherit the changed setting
        # (novels with their own override are unaffected). A new engine renders
        # the same voice differently, so an engine switch invalidates too.
        column = Novel.voice if global_voice_changed else Novel.engine
        inheriting = db.query(Novel).filter(column.is_(None)).all()
        if global_engine_changed and global_voice_changed:
            inheriting = db.query(Novel).filter(
                Novel.voice.is_(None) | Novel.engine.is_(None)).all()
        ids = {ch.id for novel in inheriting for ch in novel.chapters}
        if ids:
            remove_chapter_audio(ids)
            # Most of the cache may just have vanished; start re-earning the
            # reading window's openings now, not on the next idle tick.
            import prefetch
            prefetch.request_sweep()

    logger.info("Settings updated: engine=%s, voice=%s, speed=%.1f, mode=%s, auto_play=%s",
                settings.engine, settings.voice, settings.speed,
                settings.playback_mode, settings.auto_play)
    return settings
