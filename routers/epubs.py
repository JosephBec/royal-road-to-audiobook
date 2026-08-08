"""
EPUB upload and cover API.

Uploads land in the EPUBs folder — the same place drag-and-dropped files go —
then register immediately via the folder-sync service.
"""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
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
        dest.unlink(missing_ok=True)  # let a retry re-upload instead of 409ing forever
        raise HTTPException(status_code=500,
                            detail="Upload saved but registration failed — check server logs")
    logger.info("Uploaded EPUB: %s -> novel %d", filename, novel.id)
    return {"id": novel.id, "title": novel.title, "author": novel.author,
            "total_chapters": novel.total_chapters}


@router.get("/{novel_id}/cover")
async def epub_cover(novel_id: int, request: Request, db: Session = Depends(get_db)):
    """Serve the cover image extracted at registration time.

    Tagged with a content ETag and revalidated on every request. Novel ids are
    reused by SQLite, so this path can serve a different book after a delete —
    without a validator the browser shows the old book's artwork.

    The conditional check is done here rather than left to FileResponse, which
    derives its own ETag from the file's size and mtime; that would never match
    the content hash we hand out, so it would never answer 304.
    """
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel or not novel.rr_url.startswith("epub://"):
        raise HTTPException(status_code=404, detail="Not an EPUB book")
    stem = Path(epub_local.filename_from_url(novel.rr_url)).stem
    cover = epub_local.cover_file(stem)
    if cover is None:
        raise HTTPException(status_code=404, detail="No cover")

    etag = f'"{epub_local.cover_version(stem)}"'
    # Revalidate every time: correctness matters more than saving a request on
    # a handful of local images, and an unchanged cover costs only a 304.
    headers = {"ETag": etag, "Cache-Control": "no-cache, must-revalidate"}

    if_none_match = request.headers.get("if-none-match", "")
    if etag in [tag.strip() for tag in if_none_match.split(",")]:
        return Response(status_code=304, headers=headers)

    return FileResponse(str(cover), headers=headers)
