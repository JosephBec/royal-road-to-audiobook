"""
Text rule API: rewrite notation that reads well but speaks badly.

The preview endpoint is the important one. A regular expression is easy to get
subtly wrong, and a wrong one is only discovered by hearing it — so rules can
be tried against a real chapter and inspected before being saved.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import text_rules
from database import get_db, Chapter, Novel, TextRule
from tts import remove_chapter_audio

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["text-rules"])


class RuleRequest(BaseModel):
    kind: str = "regex"
    pattern: str
    replacement: str = ""
    note: str | None = None
    enabled: bool = True
    sort_order: int = 0


class RuleUpdate(BaseModel):
    kind: str | None = None
    pattern: str | None = None
    replacement: str | None = None
    note: str | None = None
    enabled: bool | None = None
    sort_order: int | None = None


class PreviewRequest(BaseModel):
    kind: str = "regex"
    pattern: str
    replacement: str = ""
    chapter_id: int | None = None
    text: str | None = None


def _payload(rule: TextRule) -> dict:
    return {
        "id": rule.id, "novel_id": rule.novel_id, "kind": rule.kind,
        "pattern": rule.pattern, "replacement": rule.replacement,
        "note": rule.note, "enabled": bool(rule.enabled),
        "sort_order": rule.sort_order,
    }


def _invalidate(db: Session, novel_id: int | None):
    """Rules change how text is spoken, so rendered audio is now stale."""
    query = db.query(Novel)
    if novel_id is not None:
        query = query.filter(Novel.id == novel_id)
    ids = {ch.id for novel in query.all() for ch in novel.chapters}
    if ids:
        remove_chapter_audio(ids)
        logger.info("Text rules changed — dropped audio for %d chapter(s)", len(ids))


@router.get("/novels/{novel_id}/text-rules")
async def list_rules(novel_id: int, db: Session = Depends(get_db)):
    """This novel's rules, plus the global ones that also apply to it."""
    if not db.query(Novel).filter(Novel.id == novel_id).first():
        raise HTTPException(status_code=404, detail="Novel not found")
    own = (db.query(TextRule).filter(TextRule.novel_id == novel_id)
           .order_by(TextRule.sort_order, TextRule.id).all())
    shared = (db.query(TextRule).filter(TextRule.novel_id.is_(None))
              .order_by(TextRule.sort_order, TextRule.id).all())
    return {"rules": [_payload(r) for r in own],
            "global_rules": [_payload(r) for r in shared]}


@router.post("/novels/{novel_id}/text-rules", status_code=201)
async def create_rule(novel_id: int, req: RuleRequest, db: Session = Depends(get_db)):
    if not db.query(Novel).filter(Novel.id == novel_id).first():
        raise HTTPException(status_code=404, detail="Novel not found")
    problem = text_rules.validate(req.kind, req.pattern, req.replacement)
    if problem:
        raise HTTPException(status_code=400, detail=problem)

    rule = TextRule(novel_id=novel_id, kind=req.kind, pattern=req.pattern,
                    replacement=req.replacement, note=req.note,
                    enabled=req.enabled, sort_order=req.sort_order)
    db.add(rule)
    db.commit()
    _invalidate(db, novel_id)
    return _payload(rule)


@router.patch("/text-rules/{rule_id}")
async def update_rule(rule_id: int, req: RuleUpdate, db: Session = Depends(get_db)):
    rule = db.query(TextRule).filter(TextRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    provided = req.model_fields_set
    kind = req.kind if "kind" in provided else rule.kind
    pattern = req.pattern if "pattern" in provided else rule.pattern
    replacement = req.replacement if "replacement" in provided else rule.replacement
    problem = text_rules.validate(kind, pattern, replacement)
    if problem:
        raise HTTPException(status_code=400, detail=problem)

    for field in ("kind", "pattern", "replacement", "note", "enabled", "sort_order"):
        if field in provided:
            setattr(rule, field, getattr(req, field))
    db.commit()
    _invalidate(db, rule.novel_id)
    return _payload(rule)


@router.delete("/text-rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: int, db: Session = Depends(get_db)):
    rule = db.query(TextRule).filter(TextRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    novel_id = rule.novel_id
    db.delete(rule)
    db.commit()
    _invalidate(db, novel_id)


@router.post("/text-rules/preview")
async def preview_rule(req: PreviewRequest, db: Session = Depends(get_db)):
    """Show what a rule would change, against real chapter text.

    Nothing is saved. This exists because the only other way to discover a
    subtly wrong pattern is to hear it in the middle of a chapter.
    """
    sample = req.text
    if sample is None:
        if req.chapter_id is None:
            raise HTTPException(status_code=400,
                                detail="Provide either text or chapter_id")
        chapter = db.query(Chapter).filter(Chapter.id == req.chapter_id).first()
        if not chapter:
            raise HTTPException(status_code=404, detail="Chapter not found")
        if not chapter.text:
            raise HTTPException(status_code=409,
                                detail="That chapter's text hasn't been fetched yet")
        sample = chapter.text

    try:
        count, examples = text_rules.preview(
            sample, req.kind, req.pattern, req.replacement)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"match_count": count, "examples": examples}
