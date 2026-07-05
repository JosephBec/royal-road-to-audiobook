"""Export job API: create, list, cancel, retry; Plex library listing proxy."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import export_worker
import plex
from database import get_db, ExportJob, Novel, Chapter, Settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["exports"])


class ExportRequest(BaseModel):
    start_order: int
    end_order: int
    voice: str
    speed: float


class ExportJobResponse(BaseModel):
    id: int
    novel_id: int
    novel_title: str
    start_order: int
    end_order: int
    voice: str
    speed: float
    status: str
    chapters_done: int
    chapters_total: int
    detail: str | None
    output_path: str | None
    error: str | None
    created_at: str | None
    finished_at: str | None


def _job_payload(job: ExportJob) -> ExportJobResponse:
    return ExportJobResponse(
        id=job.id, novel_id=job.novel_id, novel_title=job.novel_title,
        start_order=job.start_order, end_order=job.end_order,
        voice=job.voice, speed=job.speed, status=job.status,
        chapters_done=job.chapters_done or 0, chapters_total=job.chapters_total or 0,
        detail=job.detail, output_path=job.output_path, error=job.error,
        created_at=job.created_at.isoformat() if job.created_at else None,
        finished_at=job.finished_at.isoformat() if job.finished_at else None,
    )


@router.post("/novels/{novel_id}/export")
async def create_export(novel_id: int, req: ExportRequest, db: Session = Depends(get_db)):
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    if not (0.5 <= req.speed <= 2.0):
        raise HTTPException(status_code=400, detail="Speed must be between 0.5 and 2.0")
    if not req.voice:
        raise HTTPException(status_code=400, detail="Voice is required")
    if req.start_order > req.end_order:
        raise HTTPException(status_code=400, detail="Start chapter must be <= end chapter")

    settings = db.query(Settings).first()
    if not settings or not settings.audiobook_dir:
        raise HTTPException(status_code=400,
                            detail="Audiobook folder not set — configure it in Settings first")

    count = (db.query(Chapter)
             .filter(Chapter.novel_id == novel_id,
                     Chapter.order >= req.start_order,
                     Chapter.order <= req.end_order).count())
    if count == 0:
        raise HTTPException(status_code=400, detail="No chapters in that range")

    dupe = (db.query(ExportJob)
            .filter(ExportJob.novel_id == novel_id,
                    ExportJob.start_order == req.start_order,
                    ExportJob.end_order == req.end_order,
                    ExportJob.voice == req.voice,
                    ExportJob.speed == req.speed,
                    ExportJob.status.in_(("queued", "running"))).first())
    if dupe:
        raise HTTPException(status_code=409, detail=f"Identical export already {dupe.status}")

    job = ExportJob(novel_id=novel_id, novel_title=novel.title,
                    author=novel.author or "Unknown",
                    start_order=req.start_order, end_order=req.end_order,
                    voice=req.voice, speed=req.speed,
                    status="queued", chapters_total=count,
                    detail="queued")
    db.add(job)
    db.commit()
    db.refresh(job)
    export_worker.enqueue(job.id)
    return {"job_id": job.id}


@router.get("/exports")
async def list_exports(db: Session = Depends(get_db)):
    jobs = (db.query(ExportJob).order_by(ExportJob.created_at.desc(), ExportJob.id.desc())
            .limit(20).all())
    return {"jobs": [_job_payload(j) for j in jobs]}


@router.post("/exports/{job_id}/cancel")
async def cancel_export(job_id: int, db: Session = Depends(get_db)):
    job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status == "queued":
        job.status = "canceled"
        job.detail = "canceled"
        job.finished_at = datetime.now(timezone.utc)
        db.commit()
    elif job.status == "running":
        export_worker.request_cancel(job_id)  # worker flips status at next batch boundary
    else:
        raise HTTPException(status_code=400, detail=f"Cannot cancel a {job.status} job")
    db.refresh(job)
    return _job_payload(job)


@router.post("/exports/{job_id}/retry")
async def retry_export(job_id: int, db: Session = Depends(get_db)):
    job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in ("failed", "interrupted", "canceled"):
        raise HTTPException(status_code=400, detail=f"Cannot retry a {job.status} job")
    job.status = "queued"
    job.detail = "queued (retry — finished chapters will be reused)"
    job.error = None
    job.finished_at = None
    db.commit()
    db.refresh(job)
    export_worker.enqueue(job.id)
    return _job_payload(job)


@router.get("/plex/libraries")
async def plex_libraries(db: Session = Depends(get_db)):
    settings = db.query(Settings).first()
    if not settings or not settings.plex_url or not settings.plex_token:
        raise HTTPException(status_code=400, detail="Set Plex URL and token first")
    try:
        libs = await plex.list_libraries(settings.plex_url, settings.plex_token)
    except plex.PlexUnreachable:
        raise HTTPException(status_code=503, detail=plex.PLEX_UNREACHABLE_MSG)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Plex error: {e}")
    return {"libraries": libs}
