"""What audio is worth having on disk, and in what order to earn it.

One module answers both halves of the caching question, because two systems
have to agree on it: the prefetch worker decides what to render next, and
retention decides what to delete. When those two disagree the GPU spends
twenty minutes on a chapter the next sweep throws away.

The policy is deliberately shallow. Every chapter in the reading window gets
its opening — HEAD_START_SECONDS of audio — and nothing is ever rendered in
full in the background.

Two minutes is the whole trick. The only part of playback that makes you wait
is the model load plus the first chunk; past that Chatterbox produces audio
faster than you can listen to it, so the rest of a chapter arrives in time
whether or not it was prepared in advance. Rendering whole chapters ahead
bought nothing for that wait and cost hours of GPU on chapters that were never
opened — and it did so at the expense of the openings, which are the only part
that actually shortens the wait.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

import tts
from database import Chapter, Novel, Progress, Settings, effective_settings

logger = logging.getLogger(__name__)

# Enough audio to start listening while the rest of the chapter renders.
# Chatterbox needs ~25-60s to load plus ~10s for a first chunk, which is the
# worst moment in the whole experience; this pays that cost in advance.
HEAD_START_SECONDS = 120

# How many chapters past the current one to keep an opening for.
LOOKAHEAD = 3

# How many finished chapters behind the reader keep their audio on borrowed
# time rather than being swept the moment they leave the window. Advancing a
# chapter should not instantly destroy the one just finished.
GRACE_BEHIND = 5


@dataclass(frozen=True)
class CacheTarget:
    """A chapter that should be holding its opening, and who should speak it."""
    chapter_id: int
    url: str
    title: str
    voice: str
    engine: str | None
    reason: str          # why this earned GPU time, for the log


def head_start_satisfied(chapter_id: int) -> bool:
    """True when this chapter already holds enough audio to start on.

    A complete render counts: there is nothing to get ahead of. Short chapters
    reach that state without ever accumulating HEAD_START_SECONDS, and without
    this check the sweep would re-enter them forever, since resuming drops the
    last segment as possibly-truncated and would throw one away every tick.
    """
    if tts.temp_path_for_chapter(chapter_id).exists():
        return True
    return tts.rendered_seconds(chapter_id) >= HEAD_START_SECONDS


# ----- the window -----


def _listened_at(progress: Progress | None) -> float:
    """Sort key: when this novel was last played, 0.0 for never.

    Never-played is a real state and has to sort last. A novel imported an hour
    ago should not outrank the book you were listening to this morning just
    because its Progress row was written more recently than yours was updated.
    """
    stamp = getattr(progress, "updated_at", None)
    if stamp is None:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _active_novels(db: Session) -> list[tuple[Novel, Progress | None]]:
    """Unarchived novels, most recently listened to first.

    Archiving is how you say you are not reading a book, so archived novels
    appear in no plan and keep no audio. The ordering is the priority order
    between books: whatever you touched last is what you are most likely to
    press play on next.
    """
    novels = db.query(Novel).filter(Novel.archived.is_(False)).all()
    progress = {p.novel_id: p for p in db.query(Progress).all()}
    pairs = [(novel, progress.get(novel.id)) for novel in novels]
    pairs.sort(key=lambda pair: _listened_at(pair[1]), reverse=True)
    return pairs


def _chapter(db: Session, chapter_id: int | None) -> Chapter | None:
    if chapter_id is None:
        return None
    return db.query(Chapter).filter(Chapter.id == chapter_id).first()


def _chapters_after(db: Session, current: Chapter, limit: int) -> list[Chapter]:
    return (db.query(Chapter)
            .filter(Chapter.novel_id == current.novel_id,
                    Chapter.order > current.order)
            .order_by(Chapter.order).limit(limit).all())


def _chapter_before(db: Session, current: Chapter) -> Chapter | None:
    """The chapter you would go back to.

    The nearest lower order rather than `order - 1`: chapter repair renumbers
    to close gaps, but a library mid-repair can still have them, and an exact
    match silently finds nothing where a listener sees a previous chapter.
    """
    return (db.query(Chapter)
            .filter(Chapter.novel_id == current.novel_id,
                    Chapter.order < current.order)
            .order_by(Chapter.order.desc()).first())


def _caught_up(db: Session, current: Chapter) -> bool:
    """True when the reader is within the lookahead of the newest chapter.

    A new release only deserves to jump the queue for someone who will play it
    next. Being 21 chapters behind means the newest chapter is weeks away, and
    prioritising it would starve the chapter that is genuinely up next.
    """
    newest = (db.query(func.max(Chapter.order))
              .filter(Chapter.novel_id == current.novel_id).scalar()) or 0
    return newest - current.order <= LOOKAHEAD


def _target(chapter: Chapter, eff: dict, reason: str) -> CacheTarget:
    return CacheTarget(chapter_id=chapter.id, url=chapter.rr_url,
                       title=chapter.title, voice=eff["voice"],
                       engine=eff["engine"], reason=reason)


def _first_occurrence(targets: list[CacheTarget]) -> list[CacheTarget]:
    """Drop repeats, keeping each chapter at its highest priority."""
    seen: set[int] = set()
    unique: list[CacheTarget] = []
    for target in targets:
        if target.chapter_id in seen:
            continue
        seen.add(target.chapter_id)
        unique.append(target)
    return unique


def cache_plan(db: Session) -> list[CacheTarget]:
    """Every chapter that should hold an opening, most urgent first.

    Rebuilt from the database rather than accumulated in a queue. A queue drifts
    out of date the moment you move to a different chapter, and it dies with the
    process; the database already knows where you are, so the plan is a function
    of it and a restart costs nothing.

    New releases of favourites come first, as asked. That is safe to put above
    the rest because it is not above *playback* — pressing play holds
    interactive_synthesis() for the entire chapter render, and every target here
    yields to it continuously.
    """
    settings = db.query(Settings).first()
    releases: list[CacheTarget] = []
    window: list[CacheTarget] = []

    for novel, progress in _active_novels(db):
        current = _chapter(db, progress.chapter_id if progress else None)
        if current is None:
            continue                      # no chapters imported yet
        eff = effective_settings(novel, settings)
        ahead = _chapters_after(db, current, LOOKAHEAD)
        behind = _chapter_before(db, current)

        if novel.favorite and ahead and _caught_up(db, current):
            releases.append(_target(ahead[0], eff, "new release"))

        # The order a listener actually reaches them: what is under the
        # playhead, what autoplay runs into next, what you would go back to,
        # then the tail. Rendering the tail before the previous chapter would
        # mean the common case of pressing back is the one that waits.
        reachable = [current] + ahead[:1] + ([behind] if behind else []) + ahead[1:]
        window += [_target(c, eff, "window") for c in reachable]

    return _first_occurrence(releases + window)


def retention_sets(db: Session) -> tuple[set[int], set[int]]:
    """(keep, expiring) for tts.cleanup_temp_files.

    Keep is exactly the plan, so the sweep can never delete something the
    worker is about to render — the failure mode that made the cache churn.

    Expiring is the few chapters just behind the reader. Those are finished
    renders that cost real GPU time and might be wanted again, so they get the
    retention window rather than being dropped the instant you advance. Anything
    else, an archived novel above all, goes on the next sweep.
    """
    keep = {target.chapter_id for target in cache_plan(db)}
    expiring: set[int] = set()
    for novel, progress in _active_novels(db):
        current = _chapter(db, progress.chapter_id if progress else None)
        if current is None:
            continue
        expiring |= {c.id for c in db.query(Chapter).filter(
            Chapter.novel_id == novel.id,
            Chapter.order < current.order,
            Chapter.order >= current.order - GRACE_BEHIND,
        ).all()}
    return keep, expiring - keep
