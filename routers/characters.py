"""
Character registry and voice-script API.

This is the seam an external tagger talks to. The tagger itself — an LLM pass
that is CPU-heavy and belongs on its own machine — lives outside this app; it
reads chapter text, works out who is speaking, and PUTs the result here.

Everything below works with no tagger at all: a chapter with no script gets a
local rule-based one (narration vs quoted speech, plus explicit "X said"
tags), which is enough to render dialogue in a distinct voice.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import voice_script
from database import get_db, Novel, Chapter, Character, ChapterScript
from tts import remove_chapter_audio

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["characters"])


class CharacterRequest(BaseModel):
    name: str
    aliases: list[str] | None = None
    description: str | None = None
    voice: str | None = None
    engine: str | None = None


class CharacterUpdate(BaseModel):
    name: str | None = None
    aliases: list[str] | None = None
    description: str | None = None
    voice: str | None = None
    engine: str | None = None


class ScriptIngest(BaseModel):
    spans: list[dict]
    source: str = "external"
    # Refuse the write if the chapter text has changed since the tagger read
    # it — stale span indices would attribute lines to the wrong speaker.
    text_hash: str | None = None


class SpanOverride(BaseModel):
    kind: str | None = None
    speaker: str | None = None


def _payload(character: Character) -> dict:
    return {
        "id": character.id,
        "novel_id": character.novel_id,
        "name": character.name,
        "aliases": voice_script.character_aliases(character),
        "description": character.description,
        "voice": character.voice,
        "engine": character.engine,
    }


def _novel_or_404(db, novel_id: int) -> Novel:
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    return novel


# ===== characters =====

@router.get("/novels/{novel_id}/characters")
async def list_characters(novel_id: int, db: Session = Depends(get_db)):
    _novel_or_404(db, novel_id)
    rows = (db.query(Character)
            .filter(Character.novel_id == novel_id)
            .order_by(Character.name).all())
    return {"characters": [_payload(c) for c in rows]}


@router.post("/novels/{novel_id}/characters", status_code=201)
async def create_character(novel_id: int, req: CharacterRequest,
                           db: Session = Depends(get_db)):
    """Register a character. Idempotent on name so a tagger can re-post its
    cast list every run without creating duplicates or clobbering the voice
    you assigned."""
    _novel_or_404(db, novel_id)
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Character name is required")

    existing = (db.query(Character)
                .filter(Character.novel_id == novel_id, Character.name == name).first())
    if existing:
        if req.description and not existing.description:
            existing.description = req.description
        if req.aliases:
            merged = sorted(set(voice_script.character_aliases(existing)) | set(req.aliases))
            existing.aliases = json.dumps(merged)
        db.commit()
        return _payload(existing)

    character = Character(
        novel_id=novel_id, name=name,
        aliases=json.dumps(req.aliases) if req.aliases else None,
        description=req.description, voice=req.voice, engine=req.engine,
    )
    db.add(character)
    db.commit()
    return _payload(character)


@router.patch("/characters/{character_id}")
async def update_character(character_id: int, req: CharacterUpdate,
                           db: Session = Depends(get_db)):
    character = db.query(Character).filter(Character.id == character_id).first()
    if not character:
        raise HTTPException(status_code=404, detail="Character not found")

    provided = req.model_fields_set
    voice_changed = "voice" in provided and req.voice != character.voice
    for field in ("name", "description", "voice", "engine"):
        if field in provided:
            setattr(character, field, getattr(req, field))
    if "aliases" in provided:
        character.aliases = json.dumps(req.aliases) if req.aliases else None
    db.commit()

    if voice_changed:
        # Their lines are baked into rendered audio under the old voice.
        novel = db.query(Novel).filter(Novel.id == character.novel_id).first()
        if novel and novel.multi_voice:
            remove_chapter_audio({ch.id for ch in novel.chapters})
    return _payload(character)


@router.delete("/characters/{character_id}", status_code=204)
async def delete_character(character_id: int, db: Session = Depends(get_db)):
    character = db.query(Character).filter(Character.id == character_id).first()
    if not character:
        raise HTTPException(status_code=404, detail="Character not found")
    db.delete(character)
    db.commit()


# ===== scripts =====

def _chapter_or_404(db, chapter_id: int) -> Chapter:
    chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return chapter


@router.get("/chapters/{chapter_id}/script")
async def get_script(chapter_id: int, db: Session = Depends(get_db)):
    """The chapter's voice script, generating a rule-based one if none exists.

    Not persisted here: a GET shouldn't write, and the rule script is cheap to
    recompute. It is stored once something is actually attributed to it.
    """
    chapter = _chapter_or_404(db, chapter_id)
    if not chapter.text:
        raise HTTPException(status_code=409,
                            detail="Chapter text not fetched yet — play or prefetch it first")

    current_hash = voice_script.text_hash(chapter.text)
    row = db.query(ChapterScript).filter(ChapterScript.chapter_id == chapter_id).first()

    if row and row.text_hash == current_hash:
        spans, source, stale = voice_script.loads(row.spans), row.source, False
    else:
        spans = voice_script.build_rule_script(chapter.text)
        source = "rule"
        # A stored script built from different text can't be trusted: its span
        # indices no longer line up with the sentences.
        stale = row is not None

    return {
        "chapter_id": chapter_id,
        "text_hash": current_hash,
        "source": source,
        "stored": row is not None and not stale,
        "stale": stale,
        "stats": voice_script.script_stats(spans),
        "spans": spans,
    }


@router.put("/chapters/{chapter_id}/script")
async def put_script(chapter_id: int, req: ScriptIngest, db: Session = Depends(get_db)):
    """Ingest attributions from an external tagger.

    Manual corrections already on the chapter are preserved — the tagger is
    not allowed to overwrite a decision you made by hand.
    """
    chapter = _chapter_or_404(db, chapter_id)
    if not chapter.text:
        raise HTTPException(status_code=409, detail="Chapter text not fetched yet")

    current_hash = voice_script.text_hash(chapter.text)
    if req.text_hash and req.text_hash != current_hash:
        raise HTTPException(
            status_code=409,
            detail="Chapter text changed since tagging — re-read the text and tag again")

    sentences = len(voice_script.split_sentences(chapter.text))
    incoming = voice_script.normalize_spans(req.spans, sentences)
    if not incoming:
        raise HTTPException(status_code=400, detail="No usable spans in payload")

    row = db.query(ChapterScript).filter(ChapterScript.chapter_id == chapter_id).first()
    if row and row.text_hash == current_hash:
        merged = voice_script.merge_spans(voice_script.loads(row.spans), incoming)
    else:
        merged = voice_script.merge_spans(
            voice_script.build_rule_script(chapter.text), incoming)

    if row is None:
        row = ChapterScript(chapter_id=chapter_id)
        db.add(row)
    row.text_hash = current_hash
    row.spans = voice_script.dumps(merged)
    row.source = req.source
    db.commit()

    novel = db.query(Novel).filter(Novel.id == chapter.novel_id).first()
    if novel and novel.multi_voice:
        remove_chapter_audio({chapter_id})

    stats = voice_script.script_stats(merged)
    logger.info("Script ingested for chapter %d: %d/%d speech spans attributed",
                chapter_id, stats["attributed"], stats["speech"])
    return {"chapter_id": chapter_id, "stats": stats}


@router.patch("/chapters/{chapter_id}/script/{span_index}")
async def override_span(chapter_id: int, span_index: int, req: SpanOverride,
                        db: Session = Depends(get_db)):
    """Correct one span by hand. Marked manual, so re-tagging leaves it alone."""
    chapter = _chapter_or_404(db, chapter_id)
    if not chapter.text:
        raise HTTPException(status_code=409, detail="Chapter text not fetched yet")

    current_hash = voice_script.text_hash(chapter.text)
    row = db.query(ChapterScript).filter(ChapterScript.chapter_id == chapter_id).first()
    if row and row.text_hash == current_hash:
        spans = voice_script.loads(row.spans)
    else:
        spans = voice_script.build_rule_script(chapter.text)

    target = next((s for s in spans if s["i"] == span_index), None)
    if target is None:
        raise HTTPException(status_code=404, detail="No such span in this chapter")

    if req.kind is not None:
        if req.kind not in (voice_script.NARRATION, voice_script.SPEECH):
            raise HTTPException(status_code=400, detail="kind must be narration or speech")
        target["kind"] = req.kind
    if "speaker" in req.model_fields_set:
        target["speaker"] = req.speaker or None
        if target["speaker"]:
            target["kind"] = voice_script.SPEECH
    target["source"] = "manual"
    target["confidence"] = 1.0

    if row is None:
        row = ChapterScript(chapter_id=chapter_id)
        db.add(row)
    row.text_hash = current_hash
    row.spans = voice_script.dumps(spans)
    db.commit()

    novel = db.query(Novel).filter(Novel.id == chapter.novel_id).first()
    if novel and novel.multi_voice:
        remove_chapter_audio({chapter_id})
    return {"chapter_id": chapter_id, "span": target}
