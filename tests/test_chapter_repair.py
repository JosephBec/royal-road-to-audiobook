"""Repairing chapters duplicated by a site slug rename.

Reproduces the shape of the real corruption: a fiction renamed on Royal Road
rewrote every chapter URL, so the old URL-keyed dedup re-imported the whole
back catalogue under a second set of URLs.
"""
from datetime import datetime, timedelta, timezone

import pytest

import chapter_repair
from database import Base, Chapter, Novel, Progress


@pytest.fixture()
def db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


_next_fiction = iter(range(1, 1000))


def _novel(db, title="Hell Difficulty Tutorial", fiction=None):
    fiction = fiction if fiction is not None else next(_next_fiction)
    novel = Novel(title=title, rr_url=f"https://example.com/fiction/{fiction}/old-slug")
    db.add(novel)
    db.flush()
    novel._fiction = fiction
    return novel


def _add(db, novel, site_id, order, slug, when):
    ch = Chapter(
        novel_id=novel.id, rr_chapter_id=str(site_id), title=f"Chapter {site_id}",
        order=order,
        rr_url=f"https://example.com/fiction/{novel._fiction}/{slug}/chapter/{site_id}/x",
        published_at=when,
    )
    db.add(ch)
    db.flush()
    return ch


def _corrupt_library(db, n=5):
    """n chapters, each stored twice: once per slug."""
    novel = _novel(db)
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    originals, dupes = [], []
    for i in range(n):
        originals.append(_add(db, novel, 100 + i, i + 1, "old-slug", base + timedelta(days=i)))
    for i in range(n):
        dupes.append(_add(db, novel, 100 + i, i + 1, "new-slug", base + timedelta(days=i)))
    novel.total_chapters = n * 2
    db.commit()
    return novel, originals, dupes


def test_clean_library_is_untouched(db):
    novel = _novel(db)
    _add(db, novel, 1, 1, "slug", datetime(2025, 1, 1, tzinfo=timezone.utc))
    novel.total_chapters = 1
    db.commit()
    assert chapter_repair.repair_novel(db, Chapter, Progress, novel) is None
    assert db.query(Chapter).count() == 1


def test_duplicates_are_merged(db):
    novel, originals, dupes = _corrupt_library(db, n=5)
    report = chapter_repair.repair_novel(db, Chapter, Progress, novel)

    assert report["merged"] == 5
    assert report["total_before"] == 10
    assert report["total_after"] == 5
    assert db.query(Chapter).filter(Chapter.novel_id == novel.id).count() == 5
    assert novel.total_chapters == 5


def test_order_becomes_contiguous(db):
    novel, _, _ = _corrupt_library(db, n=5)
    chapter_repair.repair_novel(db, Chapter, Progress, novel)
    orders = sorted(c.order for c in db.query(Chapter).filter(Chapter.novel_id == novel.id))
    assert orders == [1, 2, 3, 4, 5], "duplicate orders break next/prev and the unread badge"


def test_progress_is_remapped_to_the_surviving_row(db):
    """Progress pointing at a deleted duplicate would lose the user's place."""
    novel, originals, dupes = _corrupt_library(db, n=5)
    db.add(Progress(novel_id=novel.id, chapter_id=dupes[2].id, position_seconds=1025.9))
    db.commit()
    doomed_id = dupes[2].id

    chapter_repair.repair_novel(db, Chapter, Progress, novel)

    prog = db.query(Progress).filter(Progress.novel_id == novel.id).first()
    assert prog.chapter_id != doomed_id
    assert prog.chapter_id == originals[2].id
    assert prog.position_seconds == pytest.approx(1025.9)
    assert db.query(Chapter).filter(Chapter.id == prog.chapter_id).first() is not None


def test_survivor_adopts_the_current_url(db):
    """The kept row must carry the live slug or the next crawl re-duplicates it."""
    novel, _, _ = _corrupt_library(db, n=3)
    chapter_repair.repair_novel(db, Chapter, Progress, novel)
    for ch in db.query(Chapter).filter(Chapter.novel_id == novel.id):
        assert "new-slug" in ch.rr_url


def test_scraped_text_is_not_lost(db):
    """Whichever copy was scraped keeps its cached body."""
    novel, originals, dupes = _corrupt_library(db, n=3)
    dupes[1].text = "the chapter body"
    dupes[1].word_count = 3
    db.commit()

    chapter_repair.repair_novel(db, Chapter, Progress, novel)

    kept = db.query(Chapter).filter(Chapter.id == originals[1].id).first()
    assert kept.text == "the chapter body"
    assert kept.word_count == 3


def test_repair_is_idempotent(db):
    novel, _, _ = _corrupt_library(db, n=4)
    first = chapter_repair.repair_novel(db, Chapter, Progress, novel)
    assert first is not None
    assert chapter_repair.repair_novel(db, Chapter, Progress, novel) is None


def test_unread_count_becomes_correct(db):
    """The user-visible symptom: total - progress order should be 0 at the end."""
    novel, originals, _ = _corrupt_library(db, n=5)
    db.add(Progress(novel_id=novel.id, chapter_id=originals[-1].id))
    db.commit()

    chapter_repair.repair_novel(db, Chapter, Progress, novel)

    prog = db.query(Progress).filter(Progress.novel_id == novel.id).first()
    current = db.query(Chapter).filter(Chapter.id == prog.chapter_id).first()
    assert novel.total_chapters - current.order == 0


def test_repair_all_reports_every_damaged_novel(db):
    _corrupt_library(db, n=3)
    _corrupt_library(db, n=2)
    reports = chapter_repair.repair_all(db, Chapter, Novel, Progress)
    assert len(reports) == 2
    assert chapter_repair.orphaned_audio_ids(reports)
