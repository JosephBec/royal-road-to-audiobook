"""EPUB chapter identification via the table of contents.

The old parser treated every spine document over 20 words as a chapter, so
dedications, author's notes and "Also in Series" pages were counted — which
shifted every chapter number after them. Chapters are now taken from the
navigation document / NCX, whose labels carry the author's own numbering.
"""
import pytest

from database import Base, Chapter, Novel, Progress
from scrapers import epub_local
from scrapers.epub_local import chapter_number_from_label, parse_epub_file
from tests.epub_fixtures import LONG_PARA, make_epub


# ----- label parsing -----

@pytest.mark.parametrize("label,expected", [
    ("1. Vacation", 1),
    ("12. The Muscle and the Wallet", 12),
    ("Chapter 3: Shaka-Ul Nupa", 3),
    ("chapter 45 - Something", 45),
    ("Ch. 7 Sekat", 7),
    ("Ch 108: Ona", 108),
])
def test_numbered_labels_are_recognised(label, expected):
    assert chapter_number_from_label(label) == expected


@pytest.mark.parametrize("label", [
    "Copyright",
    "Contents",
    "Also in Series",
    "Dedication",
    "Author Note / Update",
    "Glossary / Interlude - Books 1",
    "Volume 1",
    "Thank you for reading 1% Lifesteal",
    "1% Lifesteal (Volume 4)",   # a leading digit, but not a chapter number
    "",
])
def test_front_and_back_matter_is_not_numbered(label):
    assert chapter_number_from_label(label) is None


def test_percent_title_is_not_read_as_chapter_one():
    """The real regression: '1% Lifesteal (Volume 4)' must not become chapter 1."""
    assert chapter_number_from_label("1% Lifesteal (Volume 4)") is None


# ----- whole-book parsing -----

def _book_with_front_matter(path):
    """A book shaped like the user's: 3 front-matter pages, then 4 chapters."""
    return make_epub(path, title="Series Book", chapters=[
        ("Copyright", [LONG_PARA]),
        ("Also in Series", [LONG_PARA]),
        ("Dedication", [LONG_PARA]),
        ("1. Vacation", [LONG_PARA]),
        ("2. Dying Wish", [LONG_PARA]),
        ("3. Madness", [LONG_PARA]),
        ("4. Life on the Back Foot", [LONG_PARA]),
    ])


def test_front_matter_is_excluded_from_chapters(tmp_path):
    parsed = parse_epub_file(_book_with_front_matter(tmp_path / "b.epub"))
    assert len(parsed.chapters) == 4, "copyright/series/dedication must not count"


def test_chapter_numbers_match_the_author_numbering(tmp_path):
    parsed = parse_epub_file(_book_with_front_matter(tmp_path / "b.epub"))
    assert [c.number for c in parsed.chapters] == [1, 2, 3, 4]


def test_index_stays_contiguous_from_zero(tmp_path):
    """index drives playback order and chapter URLs; it must have no gaps."""
    parsed = parse_epub_file(_book_with_front_matter(tmp_path / "b.epub"))
    assert [c.index for c in parsed.chapters] == [0, 1, 2, 3]


def test_unnumbered_toc_falls_back_to_the_word_count_heuristic(tmp_path):
    """Plenty of EPUBs have no numbering; those must still yield chapters."""
    path = make_epub(tmp_path / "p.epub", chapters=[
        ("Prologue", [LONG_PARA]),
        ("The Meeting", [LONG_PARA]),
        ("The Parting", [LONG_PARA]),
    ])
    parsed = parse_epub_file(path)
    assert len(parsed.chapters) == 3
    assert all(c.number is None for c in parsed.chapters)


def test_partial_numbering_below_threshold_keeps_everything(tmp_path):
    """Two numbered labels isn't enough evidence to start dropping documents."""
    path = make_epub(tmp_path / "q.epub", chapters=[
        ("Foreword", [LONG_PARA]),
        ("1. Start", [LONG_PARA]),
        ("2. Middle", [LONG_PARA]),
    ])
    parsed = parse_epub_file(path)
    assert len(parsed.chapters) == 3, "should not trust a barely-numbered TOC"


# ----- rebuilding already-registered books -----

@pytest.fixture()
def db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(f"sqlite:///{tmp_path/'r.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_rebuild_drops_front_matter_and_keeps_progress(db, tmp_path):
    import chapter_repair

    path = _book_with_front_matter(tmp_path / "b.epub")
    filename = "b.epub"
    novel = Novel(title="Series Book", rr_url=epub_local.novel_url(filename))
    db.add(novel)
    db.flush()

    # Simulate the old parser: all 7 documents stored as chapters.
    old_titles = ["Copyright", "Also in Series", "Dedication",
                  "1. Vacation", "2. Dying Wish", "3. Madness", "4. Life on the Back Foot"]
    rows = []
    for i, title in enumerate(old_titles):
        row = Chapter(novel_id=novel.id, rr_chapter_id=str(i), title=title, order=i + 1,
                      rr_url=epub_local.chapter_url(filename, i))
        db.add(row)
        rows.append(row)
    novel.total_chapters = len(rows)
    db.flush()
    # Reading "3. Madness", stored at position 6 because of the front matter.
    db.add(Progress(novel_id=novel.id, chapter_id=rows[5].id, position_seconds=42.0))
    db.commit()

    parsed = parse_epub_file(path)
    report = chapter_repair.rebuild_epub_chapters(
        db, Chapter, Progress, novel, parsed, filename, epub_local.chapter_url)

    assert report["chapters_before"] == 7
    assert report["chapters_after"] == 4
    assert novel.total_chapters == 4

    prog = db.query(Progress).filter(Progress.novel_id == novel.id).first()
    current = db.query(Chapter).filter(Chapter.id == prog.chapter_id).first()
    assert current.title == "3. Madness", "progress must follow the chapter, not the index"
    assert current.order == 3
    assert current.chapter_number == 3
    assert prog.position_seconds == pytest.approx(42.0)


def test_rebuild_is_idempotent(db, tmp_path):
    import chapter_repair

    path = _book_with_front_matter(tmp_path / "b.epub")
    filename = "b.epub"
    novel = Novel(title="Series Book", rr_url=epub_local.novel_url(filename))
    db.add(novel)
    db.flush()
    parsed = parse_epub_file(path)
    for ch in parsed.chapters:
        db.add(Chapter(novel_id=novel.id, rr_chapter_id=str(ch.index), title=ch.title,
                       order=ch.index + 1, chapter_number=ch.number,
                       rr_url=epub_local.chapter_url(filename, ch.index)))
    novel.total_chapters = len(parsed.chapters)
    db.commit()

    assert chapter_repair.rebuild_epub_chapters(
        db, Chapter, Progress, novel, parsed, filename, epub_local.chapter_url) is None
