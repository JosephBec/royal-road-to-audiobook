"""
Chapter API routes.

Handles chapter listing, audio streaming, and synthesis status.
"""

import asyncio
import logging
import math
from datetime import datetime, timezone

import soundfile as sf
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, Novel, Chapter, Settings, Progress, effective_settings
from scrapers import get_scraper_for_url, supported_sites
from tts import (
    synthesize_chapter_to_file, synthesize_chapter_streaming,
    get_chapter_status, get_streaming_state,
    prefetch_next_chapter, cleanup_temp_files,
    temp_path_for_chapter, _segment_path,
    _aac_segment_path, SEGMENT_GAP_SECONDS,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["chapters"])


def _scraper_for(url: str):
    """Resolve the scraper for a stored chapter/novel URL or raise 400."""
    scraper = get_scraper_for_url(url)
    if not scraper:
        raise HTTPException(
            status_code=400,
            detail=f"No scraper supports this URL. Supported sites: {', '.join(supported_sites())}",
        )
    return scraper


class ChapterResponse(BaseModel):
    id: int
    novel_id: int
    title: str
    order: int
    rr_url: str
    word_count: int
    published_at: str | None
    is_current: bool = False

    class Config:
        from_attributes = True


class ChapterListResponse(BaseModel):
    chapters: list[ChapterResponse]
    total: int
    page: int
    per_page: int
    total_pages: int


@router.get("/novels/{novel_id}/chapters", response_model=ChapterListResponse)
async def list_chapters(
    novel_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=10000),
    db: Session = Depends(get_db),
):
    """Get paginated chapter list for a novel."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    settings = db.query(Settings).first()
    sort_order = effective_settings(novel, settings)["chapter_sort"]

    total = db.query(Chapter).filter(Chapter.novel_id == novel_id).count()
    order_col = Chapter.order.asc() if sort_order == "asc" else Chapter.order.desc()
    chapters = (
        db.query(Chapter)
        .filter(Chapter.novel_id == novel_id)
        .order_by(order_col)
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    progress = db.query(Progress).filter(Progress.novel_id == novel_id).first()
    current_chapter_id = progress.chapter_id if progress else None

    total_pages = max(1, (total + per_page - 1) // per_page)

    return ChapterListResponse(
        chapters=[
            ChapterResponse(
                id=ch.id,
                novel_id=ch.novel_id,
                title=ch.title,
                order=ch.order,
                rr_url=ch.rr_url,
                word_count=ch.word_count or 0,
                published_at=ch.published_at.isoformat() if ch.published_at else None,
                is_current=(ch.id == current_chapter_id),
            )
            for ch in chapters
        ],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@router.get("/chapters/{chapter_id}/stream")
async def stream_chapter(chapter_id: int, db: Session = Depends(get_db)):
    """
    Serve synthesized audio for a chapter.
    Always returns a complete WAV file (synthesizes first if needed).
    Playback speed is controlled client-side via audio.playbackRate.
    """
    chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    novel = db.query(Novel).filter(Novel.id == chapter.novel_id).first()
    settings = db.query(Settings).first()
    voice = effective_settings(novel, settings)["voice"]

    # Check if already synthesized
    existing_path = temp_path_for_chapter(chapter_id)
    if existing_path.exists():
        return FileResponse(
            path=str(existing_path),
            media_type="audio/wav",
            filename=f"chapter_{chapter_id}.wav",
        )

    # Scrape chapter text if not cached
    try:
        text = await _scraper_for(chapter.rr_url).scrape_chapter_text(chapter.rr_url)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to scrape chapter text: %s", e)
        raise HTTPException(status_code=502, detail=f"Failed to fetch chapter text: {e}")
    text = f"{chapter.title}\n\n{text}"

    # Update word count
    word_count = len(text.split())
    if chapter.word_count != word_count:
        chapter.word_count = word_count
        chapter.fetched_at = datetime.now(timezone.utc)
        db.commit()

    # Synthesize full file then serve (speed=1.0, playback speed is client-side)
    path = await synthesize_chapter_to_file(chapter_id, text, voice, 1.0)
    return FileResponse(
        path=str(path),
        media_type="audio/wav",
        filename=f"chapter_{chapter_id}.wav",
    )


@router.get("/chapters/{chapter_id}/status")
async def chapter_status(chapter_id: int, db: Session = Depends(get_db)):
    """Check if a chapter's audio is ready."""
    chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    status = get_chapter_status(chapter_id)
    return status


@router.post("/chapters/{chapter_id}/synthesize")
async def start_synthesis(chapter_id: int, db: Session = Depends(get_db)):
    """
    Start synthesizing a chapter in the background.
    For 'instant' mode, uses segment-based streaming.
    For 'full' mode, synthesizes entire file at once.
    Returns immediately; client polls /status or /segments to check progress.
    """
    chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    novel = db.query(Novel).filter(Novel.id == chapter.novel_id).first()
    settings = db.query(Settings).first()
    voice = effective_settings(novel, settings)["voice"]
    playback_mode = settings.playback_mode if settings else "full"

    # Gather all DB data we need BEFORE launching background tasks
    # (the DB session will be closed when the request returns)
    next_chapters = (
        db.query(Chapter)
        .filter(Chapter.novel_id == chapter.novel_id, Chapter.order > chapter.order)
        .order_by(Chapter.order)
        .limit(3)
        .all()
    )
    prev_ch = (
        db.query(Chapter)
        .filter(Chapter.novel_id == chapter.novel_id, Chapter.order == chapter.order - 1)
        .first()
    )

    # Pre-compute the set of chapter IDs to keep
    keep_ids = {chapter_id}
    if prev_ch:
        keep_ids.add(prev_ch.id)
    for nch in next_chapters:
        keep_ids.add(nch.id)
    # Keep every novel's in-progress chapter cached so resuming is instant
    keep_ids |= {
        p.chapter_id
        for p in db.query(Progress).filter(Progress.chapter_id.isnot(None)).all()
    }

    # Extract prefetch target info
    prefetch_id = next_chapters[0].id if next_chapters else None
    prefetch_url = next_chapters[0].rr_url if next_chapters else None
    prefetch_title = next_chapters[0].title if next_chapters else None

    async def _after_synthesis():
        """Prefetch next chapter and cleanup old files after synthesis."""
        if prefetch_id and prefetch_url and not temp_path_for_chapter(prefetch_id).exists():
            prefetch_scraper = get_scraper_for_url(prefetch_url)
            if not prefetch_scraper:
                logger.warning("No scraper for prefetch URL, skipping: %s", prefetch_url)
            else:
                try:
                    nch_text = await prefetch_scraper.scrape_chapter_text(prefetch_url)
                    nch_text = f"{prefetch_title}\n\n{nch_text}"
                    await prefetch_next_chapter(prefetch_id, nch_text, voice, 1.0)
                except Exception as e:
                    logger.warning("Prefetch failed for chapter %s: %s", prefetch_id, e)
        cleanup_temp_files(keep_ids)

    # If this chapter is already synthesized, still run the after-step:
    # the prefetch chain used to break here, leaving autoplay with a cold
    # cache every other chapter.
    status = get_chapter_status(chapter_id)
    if status["ready"]:
        asyncio.create_task(_after_synthesis())
        return {**status, "mode": "full"}

    # Scrape text
    try:
        text = await _scraper_for(chapter.rr_url).scrape_chapter_text(chapter.rr_url)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch chapter text: {e}")
    # Announce the chapter title at the start of the audio
    text = f"{chapter.title}\n\n{text}"

    # Update word count
    chapter.word_count = len(text.split())
    chapter.fetched_at = datetime.now(timezone.utc)
    db.commit()

    if playback_mode == "instant":
        async def _run_streaming():
            await synthesize_chapter_streaming(chapter_id, text, voice, 1.0)
            await _after_synthesis()
        asyncio.create_task(_run_streaming())
        return {"ready": False, "duration_seconds": None, "mode": "instant"}
    else:
        async def _run_full():
            await synthesize_chapter_to_file(chapter_id, text, voice, 1.0)
            await _after_synthesis()
        asyncio.create_task(_run_full())
        return {"ready": False, "duration_seconds": None, "mode": "full"}


@router.get("/chapters/{chapter_id}/segments")
async def get_segments(chapter_id: int, db: Session = Depends(get_db)):
    """
    Get the current streaming synthesis state for a chapter.
    Scans the filesystem for segment files to avoid GIL starvation issues.
    """
    chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    # Check full file status
    full_status = get_chapter_status(chapter_id)
    file_ready = full_status["ready"]

    # Scan disk for segment files: chapter_{id}_seg_0.wav, chapter_{id}_seg_1.wav, ...
    seg_index = 0
    segment_durations = []
    total_duration = 0.0
    while True:
        seg_path = _segment_path(chapter_id, seg_index)
        if not seg_path.exists():
            break
        try:
            info = sf.info(str(seg_path))
            segment_durations.append(info.duration)
            total_duration += info.duration
        except Exception:
            segment_durations.append(0.0)
        seg_index += 1

    # Count contiguous HLS AAC segments (encoded alongside the WAVs)
    aac_count = 0
    while _aac_segment_path(chapter_id, aac_count).exists():
        aac_count += 1

    # Check if streaming is still in progress via in-memory state
    streaming = get_streaming_state(chapter_id)
    synthesis_complete = file_ready  # full file means definitely complete
    if streaming:
        synthesis_complete = streaming.get("complete", False)

    # If no segments found but full file exists, report file-only
    if seg_index == 0 and file_ready:
        return {
            "segment_count": 0,
            "segment_durations": [],
            "aac_count": 0,
            "complete": True,
            "total_duration": full_status["duration_seconds"] or 0.0,
            "file_ready": True,
        }

    return {
        "segment_count": seg_index,
        "segment_durations": segment_durations,
        "aac_count": aac_count,
        "complete": synthesis_complete,
        "total_duration": total_duration,
        "file_ready": file_ready,
    }


@router.get("/chapters/{chapter_id}/hls.m3u8")
async def get_hls_playlist(chapter_id: int, db: Session = Depends(get_db)):
    """
    Growing (EVENT) HLS playlist of AAC segments for native iOS playback.
    Safari re-polls this until #EXT-X-ENDLIST appears, playing segments
    seamlessly — including with the screen locked.
    """
    chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    durations = []
    idx = 0
    while _aac_segment_path(chapter_id, idx).exists():
        wav = _segment_path(chapter_id, idx)
        try:
            durations.append(sf.info(str(wav)).duration + SEGMENT_GAP_SECONDS)
        except Exception:
            durations.append(SEGMENT_GAP_SECONDS)
        idx += 1

    if idx == 0:
        raise HTTPException(status_code=404, detail="No HLS segments yet")

    streaming = get_streaming_state(chapter_id)
    complete = get_chapter_status(chapter_id)["ready"]
    if streaming:
        complete = streaming.get("complete", False)

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{math.ceil(max(durations)) + 1}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:EVENT",
    ]
    for i, dur in enumerate(durations):
        lines.append(f"#EXTINF:{dur:.3f},")
        lines.append(f"/api/chapters/{chapter_id}/hls/{i}.aac")
    if complete:
        lines.append("#EXT-X-ENDLIST")

    return Response(
        content="\n".join(lines) + "\n",
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/chapters/{chapter_id}/hls/{seg_index}.aac")
async def get_hls_segment(chapter_id: int, seg_index: int):
    """Serve a single packed-audio AAC segment for HLS."""
    path = _aac_segment_path(chapter_id, seg_index)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Segment not ready")
    return FileResponse(path=str(path), media_type="audio/aac")


@router.get("/chapters/{chapter_id}/segments/{seg_index}")
async def get_segment_audio(chapter_id: int, seg_index: int, db: Session = Depends(get_db)):
    """
    Serve a single audio segment WAV file.
    """
    chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    path = _segment_path(chapter_id, seg_index)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Segment not ready")

    return FileResponse(
        path=str(path),
        media_type="audio/wav",
        filename=f"chapter_{chapter_id}_seg_{seg_index}.wav",
    )
