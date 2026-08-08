"""
One-time repair for chapters duplicated by a site slug rename.

Before sync_chapter_list keyed on rr_chapter_id, a fiction being renamed on
Royal Road rewrote every chapter URL, and the URL-keyed dedup re-imported the
entire back catalogue. Affected novels ended up with each chapter stored twice
under two URLs, a doubled total_chapters, two interleaved `order` sequences,
and an unread badge counting hundreds of chapters already listened to.

Runs from init_db(). Idempotent: a clean library is a no-op.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)


def _duplicate_groups(db, Chapter, novel_id):
    """{rr_chapter_id: [chapters...]} for ids stored more than once."""
    groups: dict[str, list] = {}
    for ch in db.query(Chapter).filter(Chapter.novel_id == novel_id).all():
        if ch.rr_chapter_id:
            groups.setdefault(ch.rr_chapter_id, []).append(ch)
    return {k: v for k, v in groups.items() if len(v) > 1}


def _sort_key(chapter):
    """Canonical reading order: publication date, else insertion order.

    published_at is the trustworthy signal for scraped novels; EPUB chapters
    have none, but their rr_chapter_id is the spine index, so falling back to
    the row id preserves the order they were parsed in.
    """
    return (chapter.published_at is None,
            chapter.published_at,
            chapter.id)


def repair_novel(db, Chapter, Progress, novel) -> dict | None:
    """Merge duplicated chapters for one novel and renumber. None if clean."""
    dups = _duplicate_groups(db, Chapter, novel.id)
    if not dups:
        return None

    removed_ids = []
    for _site_id, rows in dups.items():
        rows.sort(key=lambda c: c.id)
        keeper, extras = rows[0], rows[1:]
        # The newest row carries the current URL; the keeper carries the
        # progress and cached audio (its id is what Progress points at).
        newest_url = max(rows, key=lambda c: c.id).rr_url
        for extra in extras:
            # Move any saved progress onto the surviving row before deleting.
            db.query(Progress).filter(Progress.chapter_id == extra.id).update(
                {"chapter_id": keeper.id}, synchronize_session=False)
            if not keeper.text and extra.text:
                keeper.text = extra.text          # keep whichever copy was scraped
                keeper.word_count = extra.word_count
                keeper.fetched_at = extra.fetched_at
            removed_ids.append(extra.id)
            db.delete(extra)
        db.flush()
        if keeper.rr_url != newest_url:
            keeper.rr_url = newest_url
        db.flush()

    # Renumber what's left so `order` is contiguous again — next/prev and the
    # unread badge both read it.
    remaining = db.query(Chapter).filter(Chapter.novel_id == novel.id).all()
    remaining.sort(key=_sort_key)
    for position, chapter in enumerate(remaining, start=1):
        if chapter.order != position:
            chapter.order = position

    before = novel.total_chapters
    novel.total_chapters = len(remaining)
    db.commit()

    return {
        "novel_id": novel.id,
        "title": novel.title,
        "merged": len(removed_ids),
        "total_before": before,
        "total_after": novel.total_chapters,
        "removed_chapter_ids": removed_ids,
    }


def repair_all(db, Chapter, Novel, Progress) -> list[dict]:
    """Repair every novel with duplicated chapters. Returns per-novel reports."""
    reports = []
    for novel in db.query(Novel).all():
        report = repair_novel(db, Chapter, Progress, novel)
        if report:
            reports.append(report)
            logger.warning(
                "Repaired duplicated chapters in '%s': merged %d row(s), "
                "total_chapters %d -> %d",
                report["title"], report["merged"],
                report["total_before"], report["total_after"],
            )
    return reports


def orphaned_audio_ids(reports: list[dict]) -> set[int]:
    """Chapter ids whose rows were deleted — their cached audio is now stale."""
    return {cid for r in reports for cid in r["removed_chapter_ids"]}


# ===== EPUB re-registration after the TOC-based parser change =====

def _normalize(title: str) -> str:
    return " ".join((title or "").split()).casefold()


def rebuild_epub_chapters(db, Chapter, Progress, novel, parsed, filename,
                          chapter_url) -> dict | None:
    """Rebuild one EPUB novel's chapters from a fresh parse. None if unchanged.

    The parser used to treat every spine document over 20 words as a chapter,
    so dedications, author's notes and "Also in Series" pages were numbered
    alongside real chapters. Reading the TOC instead drops them — which shifts
    every `epub://file#index`, so the stored rows have to be rebuilt rather
    than patched. Progress is carried across by chapter title, the one thing
    stable on both sides of the change.
    """
    existing = (db.query(Chapter)
                .filter(Chapter.novel_id == novel.id)
                .order_by(Chapter.order).all())
    new_titles = [ch.title for ch in parsed.chapters]
    if [c.title for c in existing] == new_titles and len(existing) == len(new_titles):
        return None  # already matches the new parse

    prog = db.query(Progress).filter(Progress.novel_id == novel.id).first()
    old_title = None
    old_position = None
    if prog and prog.chapter_id:
        current = next((c for c in existing if c.id == prog.chapter_id), None)
        if current is not None:
            old_title = current.title
            old_position = current.order

    removed_ids = [c.id for c in existing]
    for chapter in existing:
        db.delete(chapter)
    db.flush()

    created = []
    for ch in parsed.chapters:
        row = Chapter(
            novel_id=novel.id,
            rr_chapter_id=str(ch.index),
            title=ch.title,
            order=ch.index + 1,
            chapter_number=ch.number,
            rr_url=chapter_url(filename, ch.index),
            word_count=ch.word_count,
        )
        db.add(row)
        created.append(row)
    db.flush()

    remapped_to = None
    if prog is not None:
        match = next((c for c in created if _normalize(c.title) == _normalize(old_title)), None)
        if match is None and old_position:
            # Title changed too (headings differ from TOC labels in some books):
            # fall back to the nearest surviving position rather than resetting
            # the user to chapter one.
            idx = min(max(old_position - 1, 0), len(created) - 1)
            match = created[idx] if created else None
        if match is not None:
            prog.chapter_id = match.id
            remapped_to = match.order
        else:
            prog.chapter_id = None

    novel.total_chapters = len(created)
    db.commit()

    return {
        "novel_id": novel.id,
        "title": novel.title,
        "chapters_before": len(existing),
        "chapters_after": len(created),
        "progress_was_order": old_position,
        "progress_now_order": remapped_to,
        "removed_chapter_ids": removed_ids,
    }
