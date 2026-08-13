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


# ----- multi-document chapters (regression: 38% of a book was dropped) -----

def test_chapter_spanning_several_documents_is_kept_whole(tmp_path):
    """A chapter split across files must be joined, not truncated to its first.

    Real books do this constantly — one title had 19 chapters across 74 spine
    documents. Requiring each document to carry its own numbered TOC label
    discarded every continuation page while still reporting a plausible
    chapter count, which is the worst kind of failure: silent.
    """
    path = make_epub(tmp_path / "multi.epub", chapters=[
        ("1. Opening", [LONG_PARA]),
        ("", [LONG_PARA]),            # continuation, no TOC entry of its own
        ("", [LONG_PARA]),            # another continuation
        ("2. Second", [LONG_PARA]),
        ("", [LONG_PARA]),
        ("3. Third", [LONG_PARA]),
    ])
    parsed = parse_epub_file(path)
    assert [c.number for c in parsed.chapters] == [1, 2, 3]
    # Chapter 1 owns its two continuations, so it is three paragraphs long.
    assert parsed.chapters[0].word_count > parsed.chapters[2].word_count * 2


def test_no_text_is_lost_across_numbered_chapters(tmp_path):
    path = make_epub(tmp_path / "keep.epub", chapters=[
        ("1. One", [LONG_PARA]), ("", [LONG_PARA]),
        ("2. Two", [LONG_PARA]), ("", [LONG_PARA]),
        ("3. Three", [LONG_PARA]),
    ])
    parsed = parse_epub_file(path)
    words_per_para = len(LONG_PARA.split())
    assert sum(c.word_count for c in parsed.chapters) >= words_per_para * 5


def test_image_only_documents_yield_no_chapter(tmp_path):
    """Scanned pages have no extractable text; they must not become empty
    chapters that render as silence."""
    path = make_epub(tmp_path / "img.epub", chapters=[
        ("1. Real", [LONG_PARA]),
        ("2. Scanned", ['<img src="p1.jpg"/><img src="p2.jpg"/>']),
        ("3. Real Again", [LONG_PARA]),
    ])
    parsed = parse_epub_file(path)
    assert [c.number for c in parsed.chapters] == [1, 3]


# ----- chapters separated by anchors inside one file -----

def _epub_with_anchored_chapters(path):
    """Two chapters sharing one document, split only by a #fragment.

    Calibre-style EPUBs do this constantly — in one real book 31 of 40 TOC
    entries were fragments. Ignoring the fragment hands the first chapter's
    text to the second.
    """
    from ebooklib import epub as e
    book = e.EpubBook()
    book.set_identifier("anchored")
    book.set_title("Anchored")
    book.set_language("en")
    doc = e.EpubHtml(title="Combined", file_name="combined.xhtml", lang="en")
    doc.content = (
        '<html><body>'
        f'<h1 id="c1">1 First</h1><p>{LONG_PARA}</p><p>{LONG_PARA}</p>'
        f'<h1 id="c2">2 Second</h1><p>{LONG_PARA}</p>'
        '</body></html>'
    )
    book.add_item(doc)
    book.toc = [e.Link("combined.xhtml#c1", "1 First", "c1"),
                e.Link("combined.xhtml#c2", "2 Second", "c2")]
    tail = e.EpubHtml(title="3 Third", file_name="third.xhtml", lang="en")
    tail.content = f"<html><body><h1>3 Third</h1><p>{LONG_PARA}</p></body></html>"
    book.add_item(tail)
    book.toc = list(book.toc) + [e.Link("third.xhtml", "3 Third", "c3")]
    book.add_item(e.EpubNcx())
    book.add_item(e.EpubNav())
    book.spine = ["nav", doc, tail]
    e.write_epub(str(path), book)
    return path


def test_anchored_chapters_in_one_file_are_separated(tmp_path):
    parsed = parse_epub_file(_epub_with_anchored_chapters(tmp_path / "a.epub"))
    assert [c.number for c in parsed.chapters] == [1, 2, 3]


def test_text_before_an_anchor_stays_with_the_earlier_chapter(tmp_path):
    """The exact bug: chapter 1's body sat ahead of chapter 2's anchor and was
    being attributed to chapter 2."""
    parsed = parse_epub_file(_epub_with_anchored_chapters(tmp_path / "a.epub"))
    first, second = parsed.chapters[0], parsed.chapters[1]
    assert first.word_count > second.word_count, \
        "chapter 1 has two paragraphs to chapter 2's one"


def _epub_with_phantom_anchors(path):
    """Every TOC href carries an anchor that exists in no document.

    Calibre's Kindle conversions do this: the NCX keeps synthetic position
    anchors ("2RHM0-<uuid>") that were never written into the split HTML
    files. A reader landing on a missing fragment falls back to the top of
    the file, so the entry must behave like a file-level boundary — not be
    ignored, which discards every boundary in the book and yields zero
    chapters ("12 Miles Below" regression).

    The chapter body also lives in a follow-on _split_001 file with no TOC
    entry of its own, exactly like the real book: the anchored stub holds
    only a decorative heading.
    """
    from ebooklib import epub as e
    book = e.EpubBook()
    book.set_identifier("phantom")
    book.set_title("Phantom Anchors")
    book.set_language("en")
    docs, toc = [], []
    for i, title in enumerate(["1. Only a Nightmare", "2. Prelude to Violence",
                               "3. You Should Have Left"], start=1):
        stub = e.EpubHtml(title=title, file_name=f"part{i:04d}_split_000.html",
                          lang="en", media_type="application/xhtml+xml")
        stub.content = (f'<html><body><div id="chapter-{i}">'
                        f'<span>CHAPTER</span> <span>{i}</span></div></body></html>')
        body = e.EpubHtml(title="", file_name=f"part{i:04d}_split_001.html",
                          lang="en", media_type="application/xhtml+xml")
        body.content = f"<html><body><p>{LONG_PARA}</p></body></html>"
        book.add_item(stub)
        book.add_item(body)
        docs += [stub, body]
        toc.append(e.Link(f"part{i:04d}_split_000.html#ABC{i}0-5df9f4d005b041fd",
                          title, f"num_{i}"))
    book.toc = toc
    book.add_item(e.EpubNcx())
    book.add_item(e.EpubNav())
    book.spine = ["nav"] + docs
    e.write_epub(str(path), book)
    return path


def test_phantom_anchors_fall_back_to_file_boundaries(tmp_path):
    parsed = parse_epub_file(_epub_with_phantom_anchors(tmp_path / "ph.epub"))
    assert [c.number for c in parsed.chapters] == [1, 2, 3]


def test_phantom_anchor_chapters_keep_their_split_body_text(tmp_path):
    parsed = parse_epub_file(_epub_with_phantom_anchors(tmp_path / "ph.epub"))
    words_per_para = len(LONG_PARA.split())
    for ch in parsed.chapters:
        assert ch.word_count >= words_per_para, \
            f"chapter {ch.number} lost its _split_001 body"


def test_anchor_offset_finds_id_and_name():
    from scrapers.epub_local import _anchor_offset
    assert _anchor_offset('<p id="filepos99">x</p>', "filepos99") is not None
    assert _anchor_offset("<a name='filepos99'>x</a>", "filepos99") is not None
    assert _anchor_offset('<p id="other">x</p>', "filepos99") is None
