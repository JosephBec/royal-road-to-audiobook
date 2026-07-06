"""
EPUB upload and cover API.

Uploads land in the EPUBs folder — the same place drag-and-dropped files go —
then register immediately via the folder-sync service.
"""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

import epub_library
from database import get_db, Novel
from scrapers import epub_local

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/epubs", tags=["epubs"])


@router.post("/upload", status_code=201)
async def upload_epub(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Save an uploaded EPUB into the EPUBs folder and add it to the library."""
    filename = Path(file.filename or "").name  # strip any client path components
    if not filename.lower().endswith(".epub"):
        raise HTTPException(status_code=400, detail="Only .epub files are supported")
    epub_local.EPUB_DIR.mkdir(exist_ok=True)
    dest = epub_local.EPUB_DIR / filename
    if dest.exists():
        raise HTTPException(status_code=409,
                            detail="A file with this name is already in the EPUBs folder")

    dest.write_bytes(await file.read())
    try:
        await asyncio.to_thread(epub_local.parse_epub_file, dest)
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Not a readable EPUB: {e}")

    await epub_library.sync_now(filename)
    novel = db.query(Novel).filter(Novel.rr_url == epub_local.novel_url(filename)).first()
    if not novel:
        raise HTTPException(status_code=500,
                            detail="Upload saved but registration failed — check server logs")
    logger.info("Uploaded EPUB: %s -> novel %d", filename, novel.id)
    return {"id": novel.id, "title": novel.title, "author": novel.author,
            "total_chapters": novel.total_chapters}


@router.get("/{novel_id}/cover")
async def epub_cover(novel_id: int, db: Session = Depends(get_db)):
    """Serve the cover image extracted at registration time."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel or not novel.rr_url.startswith("epub://"):
        raise HTTPException(status_code=404, detail="Not an EPUB book")
    stem = Path(epub_local.filename_from_url(novel.rr_url)).stem
    for cover in epub_local.covers_dir().glob(f"{stem}.*"):
        return FileResponse(str(cover))
    raise HTTPException(status_code=404, detail="No cover")
