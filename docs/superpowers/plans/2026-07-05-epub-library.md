# EPUB Library Folder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An `EPUBs/` folder whose contents appear as playable books in the Novel TTS library — drop a file in (or upload from the mobile web UI) and it shows up; delete it and it disappears.

**Architecture:** EPUB support plugs into the existing scraper registry as a `BaseScraper` subclass that reads local files addressed by pseudo-URLs (`epub://<quoted filename>#<chapter index>`). A background sync loop diffs the folder against the database every 5 seconds. All downstream features (streaming, text cache, progress, exports) work unchanged because they resolve text through `get_scraper_for_url(url).scrape_chapter_text(url)`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/SQLite, ebooklib + BeautifulSoup (parsing), vanilla JS frontend.

**Spec:** `docs/superpowers/specs/2026-07-05-epub-library-design.md`

## Global Constraints

- Repository: `D:\Projects\royal-road-to-audiobook` (all paths below relative to it; the current branch is `master`).
- Run tests with the project venv: `.venv\Scripts\python.exe -m pytest <path> -v` from the repo root.
- New dependency: `ebooklib==0.18` (added in Task 1). Already present: `beautifulsoup4`, `lxml`.
- `EPUB_DIR` defaults to `<repo>\EPUBs`, overridable via env var `NOVEL_TTS_EPUB_DIR` (read at import). **Consumers must access it as `epub_local.EPUB_DIR` (module attribute at call time), never `from scrapers.epub_local import EPUB_DIR`** — tests monkeypatch the attribute.
- Chapter pseudo-URL fragment is the chapter **index** (0-based); `Chapter.order` is index + 1.
- Known accepted limitation: a file replaced *while the server is off* is not detected as changed (no signature stored in DB); the UI "refresh" action covers that case by re-reading the file.

---

### Task 1: EPUB parsing core + pseudo-URL helpers

**Files:**
- Create: `scrapers/epub_local.py` (helpers + parsing; the scraper class comes in Task 2)
- Create: `tests/epub_fixtures.py`
- Create: `tests/test_epub_scraper.py`
- Modify: `requirements.txt` (after the `lxml` line)
- Modify: `tests/conftest.py`

**Interfaces:**
- Produces (used by Tasks 2–5):
  - `epub_local.EPUB_DIR: Path` and `epub_local.covers_dir() -> Path` (returns `EPUB_DIR / ".covers"`)
  - `epub_local.novel_url(filename: str) -> str`, `epub_local.chapter_url(filename: str, index: int) -> str`
  - `epub_local.filename_from_url(url: str) -> str`, `epub_local.chapter_index_from_url(url: str) -> int`
  - `epub_local.parse_epub_file(path: Path) -> ParsedBook` where `ParsedBook` has `title: str`, `author: str`, `description: str`, `cover: bytes | None`, `cover_ext: str`, `chapters: list[ParsedChapter]`; `ParsedChapter` has `title: str`, `text: str`, `index: int`, `word_count: int`. Raises `FileNotFoundError` (missing file) or `ValueError`/ebooklib errors (bad file / no chapters).
  - Test helpers `tests/epub_fixtures.py`: `make_epub(path, title=..., author=..., chapters=..., cover=..., description=...)`, `LONG_PARA` (a 30-word paragraph), `COVER_BYTES` (>1 KB fake image bytes).

- [ ] **Step 1: Install the dependency and record it**

Run: `.venv\Scripts\python.exe -m pip install ebooklib==0.18`
Expected: `Successfully installed ebooklib-0.18` (six may come along).

In `requirements.txt`, after the line `lxml==5.2.2`, add:

```
ebooklib==0.18
```

- [ ] **Step 2: Point tests at a temp EPUB folder**

`tests/conftest.py` — append to the existing env setup (the file currently sets `NOVEL_TTS_DB`):

```python
os.environ["NOVEL_TTS_EPUB_DIR"] = f"{_tmpdir}/EPUBs"
```

This keeps any test that boots the full app (lifespan starts the folder sync) away from the real `EPUBs/` folder.

- [ ] **Step 3: Write the fixture builder**

Create `tests/epub_fixtures.py`:

```python
"""Build small real EPUB files for tests (ebooklib writes EPUBs too)."""
from ebooklib import epub

# 30 words — comfortably above epub_local.MIN_CHAPTER_WORDS (20)
LONG_PARA = ("Down by the river the old miller counted his sacks of grain "
             "while the ferryman waited, whistling a tune that nobody on "
             "either bank of the river had ever heard before today.")

# Cover detection requires content > 1000 bytes; a fake JPEG header is enough
COVER_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 2000


def make_epub(path, title="Test Book", author="Test Author",
              chapters=None, cover=None, description=None):
    """Write a minimal valid EPUB. `chapters` is a list of (title, [paragraphs])."""
    if chapters is None:
        chapters = [("Chapter One", [LONG_PARA]), ("Chapter Two", [LONG_PARA])]
    book = epub.EpubBook()
    book.set_identifier(f"test-{title}")
    book.set_title(title)
    book.set_language("en")
    book.add_author(author)
    if description:
        book.add_metadata("DC", "description", description)
    if cover is not None:
        book.set_cover("cover.jpg", cover)
    items = []
    for i, (ch_title, paragraphs) in enumerate(chapters, start=1):
        item = epub.EpubHtml(title=ch_title, file_name=f"ch{i}.xhtml", lang="en")
        body = "".join(f"<p>{p}</p>" for p in paragraphs)
        item.content = f"<html><body><h1>{ch_title}</h1>{body}</body></html>"
        book.add_item(item)
        items.append(item)
    book.toc = items
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + items
    epub.write_epub(str(path), book)
    return path
```

- [ ] **Step 4: Write the failing tests**

Create `tests/test_epub_scraper.py`:

```python
"""Local EPUB source: pseudo-URLs and file parsing."""
import pytest

from tests.epub_fixtures import make_epub, LONG_PARA, COVER_BYTES


def test_url_roundtrip():
    from scrapers import epub_local
    name = "Mother of Learning — Book 1.epub"
    url = epub_local.novel_url(name)
    assert url.startswith("epub://")
    assert "#" not in url                      # '#' in names must be quoted
    assert epub_local.filename_from_url(url) == name
    ch = epub_local.chapter_url(name, 12)
    assert epub_local.filename_from_url(ch) == name
    assert epub_local.chapter_index_from_url(ch) == 12


def test_parse_epub_file(tmp_path):
    from scrapers import epub_local
    path = make_epub(tmp_path / "book.epub", title="My Book", author="Jane Doe",
                     description="A story.", cover=COVER_BYTES)
    parsed = epub_local.parse_epub_file(path)
    assert parsed.title == "My Book"
    assert parsed.author == "Jane Doe"
    assert parsed.description == "A story."
    assert [ch.title for ch in parsed.chapters] == ["Chapter One", "Chapter Two"]
    assert parsed.chapters[0].index == 0
    assert all(ch.word_count >= 20 for ch in parsed.chapters)
    assert LONG_PARA.split()[0] in parsed.chapters[0].text
    assert parsed.cover is not None and len(parsed.cover) > 1000


def test_parse_missing_and_invalid(tmp_path):
    from scrapers import epub_local
    with pytest.raises(FileNotFoundError):
        epub_local.parse_epub_file(tmp_path / "nope.epub")
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"this is not a zip archive")
    with pytest.raises(Exception):
        epub_local.parse_epub_file(bad)
```

- [ ] **Step 5: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests\test_epub_scraper.py -v`
Expected: FAIL — `ModuleNotFoundError`/`AttributeError` (no `scrapers.epub_local` yet).

- [ ] **Step 6: Implement the module**

Create `scrapers/epub_local.py`. The parsing functions are ported from
`D:\Projects\Ebook-to-Audiobook\src\epub_parser.py` (minus CLI-only parts):

```python
"""
Local EPUB source: pseudo-URL helpers and file parsing.

Books in the EPUBs folder flow through the same scraper machinery as web
novels by using pseudo-URLs:

    novel:   epub://Mother%20of%20Learning.epub
    chapter: epub://Mother%20of%20Learning.epub#12   (12 = 0-based chapter index)

EPUB_DIR defaults to <repo>/EPUBs (override with NOVEL_TTS_EPUB_DIR). Access
it as `epub_local.EPUB_DIR` — a module attribute read at call time — so tests
can monkeypatch it.
"""

import logging
import os
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

logger = logging.getLogger(__name__)

EPUB_DIR = Path(os.environ.get(
    "NOVEL_TTS_EPUB_DIR",
    str(Path(__file__).resolve().parent.parent / "EPUBs"),
))
URL_SCHEME = "epub://"
MIN_CHAPTER_WORDS = 20  # spine items below this are front matter, not chapters


def covers_dir() -> Path:
    return EPUB_DIR / ".covers"


# ===== Pseudo-URL helpers =====

def novel_url(filename: str) -> str:
    return URL_SCHEME + quote(filename)


def chapter_url(filename: str, index: int) -> str:
    return f"{novel_url(filename)}#{index}"


def filename_from_url(url: str) -> str:
    return unquote(url[len(URL_SCHEME):].split("#", 1)[0])


def chapter_index_from_url(url: str) -> int:
    return int(url.split("#", 1)[1])


# ===== Parsing =====

@dataclass
class ParsedChapter:
    title: str
    text: str
    index: int
    word_count: int = 0

    def __post_init__(self):
        self.word_count = len(self.text.split())


@dataclass
class ParsedBook:
    title: str = "Unknown Title"
    author: str = "Unknown Author"
    description: str = ""
    chapters: list[ParsedChapter] = field(default_factory=list)
    cover: bytes | None = None
    cover_ext: str = "jpg"


def clean_html_to_text(html_content: str) -> str:
    """Strip tags, normalize whitespace, keep paragraph breaks as blank lines."""
    soup = BeautifulSoup(html_content, "lxml")
    for element in soup(["script", "style", "head", "meta", "link"]):
        element.decompose()
    text = soup.get_text(separator="\n")
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            lines.append(re.sub(r"[ \t]+", " ", stripped))
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean_chapter_title(title: str) -> str:
    """'34 34. Problem' -> '34. Problem' (some EPUBs double the number)."""
    match = re.match(r"^(\d+)\s+(\d+[\.\):\-\s])", title)
    if match:
        leading = int(match.group(1))
        second = int(re.match(r"\d+", match.group(2)).group())
        if leading == second:
            return title[match.end(1):].lstrip()
    return title


def extract_chapter_title(html_content: str, fallback_title: str) -> str:
    """First h1-h4 heading, else the fallback."""
    soup = BeautifulSoup(html_content, "lxml")
    for tag in ["h1", "h2", "h3", "h4"]:
        heading = soup.find(tag)
        if heading:
            title = heading.get_text(strip=True)
            if title and len(title) < 200:
                return clean_chapter_title(title)
    return fallback_title


def _cover_ext(media_type: str, filename: str) -> str:
    if "png" in media_type or filename.lower().endswith(".png"):
        return "png"
    if "gif" in media_type or filename.lower().endswith(".gif"):
        return "gif"
    if "webp" in media_type or filename.lower().endswith(".webp"):
        return "webp"
    return "jpg"


def extract_cover_image(book: epub.EpubBook) -> tuple[bytes | None, str]:
    """Try several strategies; EPUBs are wildly inconsistent about covers."""
    # EPUB3 cover-image property
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        try:
            if getattr(item, "properties", None) and "cover-image" in item.properties:
                return item.get_content(), _cover_ext(item.media_type, item.get_name())
        except Exception:
            pass
    # OPF <meta name="cover" content="id">
    cover_meta = book.get_metadata("OPF", "cover")
    if cover_meta:
        cover_id = cover_meta[0][1].get("content", "") if len(cover_meta[0]) > 1 else ""
        if cover_id:
            for item in book.get_items():
                if item.get_id() == cover_id:
                    return item.get_content(), _cover_ext(item.media_type, item.get_name())
    # image items named/id'd "cover"
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        if "cover" in item.get_name().lower() or "cover" in (item.get_id() or "").lower():
            return item.get_content(), _cover_ext(item.media_type, item.get_name())
    # a cover XHTML page wrapping an <img>
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        if "cover" in item.get_name().lower() or "cover" in (item.get_id() or "").lower():
            try:
                soup = BeautifulSoup(item.get_content().decode("utf-8", errors="replace"), "lxml")
                img = soup.find("img")
                if img and img.get("src"):
                    tail = img["src"].split("/")[-1]
                    for img_item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
                        if img_item.get_name().endswith(tail):
                            return img_item.get_content(), _cover_ext(img_item.media_type, img_item.get_name())
            except Exception:
                pass
    # EpubCover items (not typed ITEM_IMAGE by ebooklib)
    for item in book.get_items():
        if (item.get_id() or "").lower() == "cover" or type(item).__name__ == "EpubCover":
            try:
                content = item.get_content()
                if content and len(content) > 1000:  # real image, not markup
                    return content, _cover_ext(getattr(item, "media_type", ""), item.get_name())
            except Exception:
                pass
    return None, "jpg"


def parse_epub_file(path: Path, min_chapter_words: int = MIN_CHAPTER_WORDS) -> ParsedBook:
    """Parse an EPUB into metadata + chapters. Raises on missing/broken files
    and on books with no extractable chapters."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"EPUB file not found: {path}")

    book = epub.read_epub(str(path), options={"ignore_ncx": False})
    parsed = ParsedBook()

    title = book.get_metadata("DC", "title")
    if title:
        parsed.title = title[0][0]
    creator = book.get_metadata("DC", "creator")
    if creator:
        parsed.author = creator[0][0]
    desc = book.get_metadata("DC", "description")
    if desc:
        parsed.description = clean_html_to_text(desc[0][0])

    parsed.cover, parsed.cover_ext = extract_cover_image(book)

    index = 0
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        try:
            content = item.get_content().decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning("Failed to decode item %s: %s", item.get_name(), e)
            continue
        text = clean_html_to_text(content)
        if len(text.split()) < min_chapter_words:
            continue  # cover page, TOC, copyright, etc.
        parsed.chapters.append(ParsedChapter(
            title=extract_chapter_title(content, f"Chapter {index + 1}"),
            text=text,
            index=index,
        ))
        index += 1

    if not parsed.chapters:
        raise ValueError(f"No chapters with sufficient content found in: {path.name}")
    return parsed
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests\test_epub_scraper.py -v`
Expected: 3 passed.

- [ ] **Step 8: Commit**

```bash
git add requirements.txt tests/conftest.py tests/epub_fixtures.py tests/test_epub_scraper.py scrapers/epub_local.py
git commit -m "feat: EPUB parsing core and epub:// pseudo-URL helpers"
```

---

### Task 2: EpubScraper — plug EPUBs into the scraper registry

**Files:**
- Modify: `scrapers/epub_local.py` (append the scraper class)
- Modify: `tests/test_epub_scraper.py` (append tests)

**Interfaces:**
- Consumes: Task 1's helpers (`parse_epub_file`, URL helpers, `EPUB_DIR`).
- Produces: class `EpubScraper(BaseScraper)` with `name = "epub"`, matching `^epub://`. Auto-discovered by `scrapers.discover_scrapers()` — playback (`routers/chapters.py`), prefetch (`library_sync.py`), refresh (`routers/novels.py:refresh_novel`), and exports (`export_worker.py`) all start working for `epub://` URLs with **zero changes** to those files.
  - `scrape_novel_metadata(url) -> {title, author, cover_url: None, description, rr_url}` (`cover_url` is set later by the sync service, once a novel id exists)
  - `scrape_chapter_list(url) -> [{title, rr_url, rr_chapter_id: str(index), order: index+1, published_at: None}]`
  - `scrape_chapter_text(url) -> str` (raises `ValueError` for an unknown index)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_epub_scraper.py`:

```python
@pytest.fixture()
def library(tmp_path, monkeypatch):
    from scrapers import epub_local
    lib = tmp_path / "EPUBs"
    lib.mkdir()
    monkeypatch.setattr(epub_local, "EPUB_DIR", lib)
    return lib


def test_scraper_registered():
    from scrapers import get_scraper_for_url
    scraper = get_scraper_for_url("epub://Some%20Book.epub")
    assert scraper is not None and scraper.name == "epub"
    assert get_scraper_for_url("https://example.com/") is None  # no false match
    assert get_scraper_for_url("https://www.royalroad.com/fiction/1/x").name == "Royal Road"


def test_scraper_interface(library):
    import asyncio
    from scrapers import epub_local
    make_epub(library / "My Book.epub", title="My Book", author="Jane Doe")
    scraper = epub_local.EpubScraper()
    url = epub_local.novel_url("My Book.epub")

    meta = asyncio.run(scraper.scrape_novel_metadata(url))
    assert meta["title"] == "My Book"
    assert meta["author"] == "Jane Doe"
    assert meta["rr_url"] == url
    assert meta["cover_url"] is None

    chapters = asyncio.run(scraper.scrape_chapter_list(url))
    assert [c["order"] for c in chapters] == [1, 2]
    assert chapters[0]["rr_url"] == epub_local.chapter_url("My Book.epub", 0)
    assert chapters[0]["rr_chapter_id"] == "0"
    assert chapters[0]["published_at"] is None

    text = asyncio.run(scraper.scrape_chapter_text(chapters[1]["rr_url"]))
    assert LONG_PARA.split()[0] in text


def test_scraper_missing_file_and_bad_index(library):
    import asyncio
    from scrapers import epub_local
    scraper = epub_local.EpubScraper()
    with pytest.raises(FileNotFoundError):
        asyncio.run(scraper.scrape_chapter_text("epub://gone.epub#0"))
    make_epub(library / "small.epub")
    with pytest.raises(ValueError):
        asyncio.run(scraper.scrape_chapter_text(epub_local.chapter_url("small.epub", 99)))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests\test_epub_scraper.py -v`
Expected: the 3 new tests FAIL (`AttributeError: ... has no attribute 'EpubScraper'` / registry returns `None`); the 3 Task-1 tests still pass.

- [ ] **Step 3: Implement the scraper class**

Append to `scrapers/epub_local.py` (add `import asyncio` to the imports and `from scrapers.base import BaseScraper` below them):

```python
import asyncio

from scrapers.base import BaseScraper


class EpubScraper(BaseScraper):
    """Reads books from the local EPUBs folder instead of the web."""

    name = "epub"
    url_patterns = [re.compile(r"^epub://")]

    async def scrape_novel_metadata(self, url: str) -> dict:
        filename = filename_from_url(url)
        parsed = await asyncio.to_thread(parse_epub_file, EPUB_DIR / filename)
        return {
            "title": parsed.title,
            "author": parsed.author,
            "cover_url": None,  # set by epub_library once the novel row exists
            "description": parsed.description,
            "rr_url": novel_url(filename),
        }

    async def scrape_chapter_list(self, novel_url_: str) -> list[dict]:
        filename = filename_from_url(novel_url_)
        parsed = await asyncio.to_thread(parse_epub_file, EPUB_DIR / filename)
        return [{
            "title": ch.title,
            "rr_url": chapter_url(filename, ch.index),
            "rr_chapter_id": str(ch.index),
            "order": ch.index + 1,
            "published_at": None,
        } for ch in parsed.chapters]

    async def scrape_chapter_text(self, chapter_url_: str) -> str:
        filename = filename_from_url(chapter_url_)
        index = chapter_index_from_url(chapter_url_)
        parsed = await asyncio.to_thread(parse_epub_file, EPUB_DIR / filename)
        for ch in parsed.chapters:
            if ch.index == index:
                return ch.text
        raise ValueError(f"Chapter {index} not found in {filename}")
```

(Move the `import asyncio` up with the other imports rather than mid-file.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests\test_epub_scraper.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add scrapers/epub_local.py tests/test_epub_scraper.py
git commit -m "feat: EpubScraper - local EPUB files as a scraper source"
```

---

### Task 3: Folder sync service

**Files:**
- Create: `epub_library.py`
- Create: `tests/test_epub_library.py`

**Interfaces:**
- Consumes: Task 1/2 (`epub_local` helpers), `database.SessionLocal/Novel/Chapter`, `library_sync.sync_chapter_list(db, novel, chapter_list) -> int`, `tts.remove_chapter_audio(chapter_ids: set[int])`.
- Produces (used by Tasks 4–5):
  - `epub_library.start()` / `epub_library.stop()` — lifespan hooks; `start()` creates `EPUB_DIR` and `.covers/` and launches the poll loop (`POLL_SECONDS = 5.0`).
  - `async epub_library.sync_once()` — one folder⇄DB diff pass.
  - `async epub_library.sync_now(filename: str)` — register a just-uploaded file immediately (skips the two-poll stability wait).
  - `epub_library.forget(filename: str)` — drop in-memory tracking after the app itself deletes a file (UI delete).
  - `epub_library.reset()` — clear in-memory state (tests only).
  - Registered novels get `cover_url = f"/api/epubs/{novel.id}/cover"` and a cover file at `covers_dir() / f"{Path(filename).stem}.{ext}"` (the Task 4 endpoint serves these).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_epub_library.py`:

```python
"""Folder sync: files appearing/vanishing/changing drive the library."""
import asyncio
import os

import pytest

from tests.epub_fixtures import make_epub, LONG_PARA, COVER_BYTES


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import database
    database.init_db()
    from scrapers import epub_local
    import epub_library
    lib = tmp_path / "EPUBs"
    lib.mkdir()
    (lib / ".covers").mkdir()
    monkeypatch.setattr(epub_local, "EPUB_DIR", lib)
    removed_audio = []
    monkeypatch.setattr(epub_library, "remove_chapter_audio",
                        lambda ids: removed_audio.append(set(ids)))
    epub_library.reset()
    yield lib, removed_audio
    db = database.SessionLocal()
    for n in db.query(database.Novel).filter(database.Novel.rr_url.like("epub://%")).all():
        db.delete(n)
    db.commit(); db.close()


def _sync():
    import epub_library
    asyncio.run(epub_library.sync_once())


def _epub_novels():
    """Snapshot epub novels as plain dicts (session-independent)."""
    import database
    db = database.SessionLocal()
    try:
        rows = db.query(database.Novel).filter(database.Novel.rr_url.like("epub://%")).all()
        return [{"id": n.id, "title": n.title, "total_chapters": n.total_chapters,
                 "cover_url": n.cover_url,
                 "chapter_ids": [c.id for c in sorted(n.chapters, key=lambda c: c.order)]}
                for n in rows]
    finally:
        db.close()


def test_new_file_registers_after_two_stable_polls(env):
    lib, _ = env
    make_epub(lib / "Book One.epub", title="Book One", cover=COVER_BYTES)
    _sync()
    assert _epub_novels() == []          # first sighting: waiting for stability
    _sync()
    novels = _epub_novels()
    assert len(novels) == 1
    assert novels[0]["title"] == "Book One"
    assert novels[0]["total_chapters"] == 2
    assert novels[0]["cover_url"] == f"/api/epubs/{novels[0]['id']}/cover"
    assert (lib / ".covers" / "Book One.jpg").exists()


def test_file_still_copying_is_not_registered(env):
    lib, _ = env
    path = make_epub(lib / "Partial.epub")
    _sync()
    with open(path, "ab") as f:          # size changes between polls
        f.write(b"\x00" * 10)
    _sync()
    assert _epub_novels() == []
    _sync()
    assert len(_epub_novels()) == 1


def test_removed_file_deletes_novel_progress_and_audio(env):
    lib, removed_audio = env
    make_epub(lib / "Gone.epub", cover=COVER_BYTES)
    _sync(); _sync()
    novel = _epub_novels()[0]
    import database
    db = database.SessionLocal()
    db.add(database.Progress(novel_id=novel["id"], chapter_id=novel["chapter_ids"][0]))
    db.commit(); db.close()

    (lib / "Gone.epub").unlink()
    _sync()
    assert _epub_novels() == []
    assert removed_audio and removed_audio[0] == set(novel["chapter_ids"])
    assert not (lib / ".covers" / "Gone.jpg").exists()
    db = database.SessionLocal()
    assert db.query(database.Progress).filter_by(novel_id=novel["id"]).first() is None
    db.close()


def test_replaced_file_resyncs_chapters_keeps_progress(env):
    lib, _ = env
    path = make_epub(lib / "Series.epub",
                     chapters=[("Ch 1", [LONG_PARA]), ("Ch 2", [LONG_PARA])])
    _sync(); _sync()
    novel = _epub_novels()[0]
    import database
    db = database.SessionLocal()
    db.add(database.Progress(novel_id=novel["id"], chapter_id=novel["chapter_ids"][1]))
    db.commit(); db.close()

    make_epub(lib / "Series.epub",
              chapters=[("Ch 1", [LONG_PARA]), ("Ch 2", [LONG_PARA]), ("Ch 3", [LONG_PARA])])
    os.utime(path)                        # ensure mtime moves even on coarse clocks
    _sync()
    novel2 = _epub_novels()[0]
    assert novel2["id"] == novel["id"]    # same row — progress preserved
    assert novel2["total_chapters"] == 3
    db = database.SessionLocal()
    prog = db.query(database.Progress).filter_by(novel_id=novel["id"]).first()
    assert prog is not None and prog.chapter_id == novel["chapter_ids"][1]
    db.close()


def test_corrupt_file_skipped_without_crash(env):
    lib, _ = env
    (lib / "junk.epub").write_bytes(b"not really an epub")
    _sync(); _sync(); _sync()
    assert _epub_novels() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests\test_epub_library.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'epub_library'`.

- [ ] **Step 3: Implement the sync service**

Create `epub_library.py`:

```python
"""
EPUBs folder sync service.

The EPUBs folder is the source of truth for locally-added books: files that
appear become library novels, files that disappear take their novel (and
progress) with them, and a file replaced under the same name re-syncs its
chapter list while keeping progress.

Runs as an asyncio loop started from the app lifespan. A new file must hold
a stable size/mtime across two consecutive polls before registration so a
file still being copied in is never parsed. A file whose parse fails is
skipped and retried only when it changes on disk.
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from database import SessionLocal, Novel, Chapter
from library_sync import sync_chapter_list
from scrapers import epub_local
from tts import remove_chapter_audio

logger = logging.getLogger(__name__)

POLL_SECONDS = 5.0

_task: asyncio.Task | None = None
_seen: dict[str, tuple[int, int]] = {}      # filename -> (mtime_ns, size) last handled
_pending: dict[str, tuple[int, int]] = {}   # new files awaiting a stable signature
# asyncio.Lock binds to the loop that first acquires it; tests run many
# short-lived loops, so keep one lock per event loop.
_locks: dict[int, asyncio.Lock] = {}


def _lock() -> asyncio.Lock:
    return _locks.setdefault(id(asyncio.get_running_loop()), asyncio.Lock())


def start():
    """Create folders and start the polling loop (called from app lifespan)."""
    global _task
    epub_local.EPUB_DIR.mkdir(exist_ok=True)
    epub_local.covers_dir().mkdir(exist_ok=True)
    _task = asyncio.create_task(_loop())
    logger.info("EPUB folder sync watching %s", epub_local.EPUB_DIR)


def stop():
    if _task and not _task.done():
        _task.cancel()


def reset():
    """Clear in-memory state (tests only)."""
    _seen.clear()
    _pending.clear()
    _locks.clear()


def forget(filename: str):
    """Drop tracking for a file the app itself deleted (UI delete)."""
    _seen.pop(filename, None)
    _pending.pop(filename, None)


async def _loop():
    while True:
        try:
            await sync_once()
        except Exception:
            logger.exception("EPUB folder sync pass failed")
        await asyncio.sleep(POLL_SECONDS)


async def sync_now(filename: str):
    """Register a just-uploaded file immediately — the upload endpoint only
    calls this after the file is fully written, so no stability wait."""
    path = epub_local.EPUB_DIR / filename
    st = path.stat()
    _pending[filename] = (st.st_mtime_ns, st.st_size)
    await sync_once()


async def sync_once():
    """One diff pass: folder contents vs epub:// novels in the database."""
    async with _lock():
        db = SessionLocal()
        try:
            files = {p.name: p.stat() for p in epub_local.EPUB_DIR.glob("*.epub")}
            novels = {
                epub_local.filename_from_url(n.rr_url): n
                for n in db.query(Novel).filter(Novel.rr_url.like("epub://%")).all()
            }

            for name in set(novels) - set(files):
                _remove(db, novels[name], name)
            for name in set(_seen) - set(files):
                _seen.pop(name, None)
            for name in set(_pending) - set(files):
                _pending.pop(name, None)

            for name, st in files.items():
                sig = (st.st_mtime_ns, st.st_size)
                if _seen.get(name) == sig:
                    continue  # unchanged since last handled (or known-bad)
                novel = novels.get(name)
                if novel is not None:
                    if name in _seen:  # replaced/extended edition
                        await _resync(db, novel, name)
                    _seen[name] = sig  # startup snapshot, or record the resync
                    continue
                if _pending.get(name) != sig:
                    _pending[name] = sig  # first sighting; wait for stability
                    continue
                del _pending[name]
                await _register(db, name)
                _seen[name] = sig  # success or parse failure: retry only on change
        finally:
            db.close()


async def _register(db, filename: str):
    path = epub_local.EPUB_DIR / filename
    try:
        parsed = await asyncio.to_thread(epub_local.parse_epub_file, path)
    except Exception:
        logger.exception("Cannot parse %s — skipping until the file changes", filename)
        return
    novel = Novel(
        title=parsed.title,
        author=parsed.author,
        rr_url=epub_local.novel_url(filename),
        description=parsed.description,
    )
    db.add(novel)
    db.flush()
    if parsed.cover:
        epub_local.covers_dir().mkdir(exist_ok=True)
        cover_path = epub_local.covers_dir() / f"{Path(filename).stem}.{parsed.cover_ext}"
        cover_path.write_bytes(parsed.cover)
        novel.cover_url = f"/api/epubs/{novel.id}/cover"
    for ch in parsed.chapters:
        db.add(Chapter(
            novel_id=novel.id,
            rr_chapter_id=str(ch.index),
            title=ch.title,
            order=ch.index + 1,
            rr_url=epub_local.chapter_url(filename, ch.index),
            word_count=ch.word_count,
        ))
    novel.total_chapters = len(parsed.chapters)
    novel.last_refreshed = datetime.now(timezone.utc)
    db.commit()
    logger.info("Registered EPUB: %s (%d chapters)", parsed.title, novel.total_chapters)


async def _resync(db, novel: Novel, filename: str):
    path = epub_local.EPUB_DIR / filename
    try:
        parsed = await asyncio.to_thread(epub_local.parse_epub_file, path)
    except Exception:
        logger.exception("Cannot re-parse %s — keeping existing chapters", filename)
        return
    chapter_list = [{
        "title": ch.title,
        "rr_url": epub_local.chapter_url(filename, ch.index),
        "rr_chapter_id": str(ch.index),
        "order": ch.index + 1,
        "published_at": None,
    } for ch in parsed.chapters]
    new_count = sync_chapter_list(db, novel, chapter_list)
    if new_count:
        logger.info("EPUB %s: %d new chapter(s)", filename, new_count)


def _remove(db, novel: Novel, filename: str):
    title = novel.title
    remove_chapter_audio({ch.id for ch in novel.chapters})
    for cover in epub_local.covers_dir().glob(f"{Path(filename).stem}.*"):
        cover.unlink(missing_ok=True)
    db.delete(novel)
    db.commit()
    forget(filename)
    logger.info("EPUB file removed — deleted novel: %s", title)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests\test_epub_library.py -v`
Expected: 5 passed.

- [ ] **Step 5: Run the earlier suites too (regression)**

Run: `.venv\Scripts\python.exe -m pytest tests\test_epub_scraper.py tests\test_epub_library.py -v`
Expected: 11 passed.

- [ ] **Step 6: Commit**

```bash
git add epub_library.py tests/test_epub_library.py
git commit -m "feat: EPUBs folder sync service - folder is the source of truth"
```

---

### Task 4: Upload + cover endpoints, app wiring

**Files:**
- Create: `routers/epubs.py`
- Modify: `main.py` (router import/include + lifespan start/stop)
- Create: `tests/test_epubs_api.py`

**Interfaces:**
- Consumes: `epub_library.sync_now/start/stop/reset`, `epub_local` helpers, `database.get_db/Novel`.
- Produces:
  - `POST /api/epubs/upload` (multipart field `file`) → 201 `{id, title, author, total_chapters}`; 400 bad extension/unparseable; 409 duplicate filename.
  - `GET /api/epubs/{novel_id}/cover` → image file or 404.
  - App lifespan starts/stops the sync loop.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_epubs_api.py`:

```python
"""Upload and cover endpoints (UI-delete test lands in Task 5)."""
import pytest
from fastapi.testclient import TestClient

from tests.epub_fixtures import make_epub, COVER_BYTES


@pytest.fixture()
def client(monkeypatch, tmp_path):
    import export_worker
    import epub_library
    from scrapers import epub_local
    monkeypatch.setattr(export_worker, "start_worker", lambda: None)
    monkeypatch.setattr(epub_library, "start", lambda: None)   # no bg loop in tests
    monkeypatch.setattr(epub_library, "remove_chapter_audio", lambda ids: None)
    lib = tmp_path / "EPUBs"
    lib.mkdir()
    (lib / ".covers").mkdir()
    monkeypatch.setattr(epub_local, "EPUB_DIR", lib)
    epub_library.reset()
    from main import app
    with TestClient(app) as c:
        c.epub_dir = lib
        yield c
    import database
    db = database.SessionLocal()
    for n in db.query(database.Novel).filter(database.Novel.rr_url.like("epub://%")).all():
        db.delete(n)
    db.commit(); db.close()


def _upload(client, tmp_path, name="My Book.epub", **make_kwargs):
    src = tmp_path / "upload-src.epub"
    make_epub(src, **make_kwargs)
    with open(src, "rb") as f:
        return client.post("/api/epubs/upload",
                           files={"file": (name, f, "application/epub+zip")})


def test_upload_registers_book(client, tmp_path):
    resp = _upload(client, tmp_path, title="Uploaded Book", cover=COVER_BYTES)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "Uploaded Book"
    assert body["total_chapters"] == 2
    assert (client.epub_dir / "My Book.epub").exists()

    novels = client.get("/api/novels").json()
    mine = next(n for n in novels if n["id"] == body["id"])
    assert mine["source"] == "epub"
    assert mine["cover_url"] == f"/api/epubs/{body['id']}/cover"
    assert client.get(f"/api/epubs/{body['id']}/cover").status_code == 200


def test_upload_duplicate_name_conflicts(client, tmp_path):
    assert _upload(client, tmp_path).status_code == 201
    assert _upload(client, tmp_path).status_code == 409


def test_upload_invalid_file_rejected(client, tmp_path):
    resp = client.post("/api/epubs/upload",
                       files={"file": ("bad.epub", b"garbage", "application/epub+zip")})
    assert resp.status_code == 400
    assert not (client.epub_dir / "bad.epub").exists()


def test_upload_wrong_extension_rejected(client, tmp_path):
    resp = client.post("/api/epubs/upload",
                       files={"file": ("book.mobi", b"whatever", "application/octet-stream")})
    assert resp.status_code == 400


def test_cover_404_for_non_epub_novel(client):
    import database
    db = database.SessionLocal()
    novel = database.Novel(title="Web", rr_url="https://www.royalroad.com/fiction/424242/web")
    db.add(novel); db.commit()
    nid = novel.id
    db.close()
    assert client.get(f"/api/epubs/{nid}/cover").status_code == 404
    db = database.SessionLocal()
    db.query(database.Novel).filter_by(id=nid).delete()
    db.commit(); db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests\test_epubs_api.py -v`
Expected: FAIL — 404s from `/api/epubs/upload` (router doesn't exist).

- [ ] **Step 3: Implement the router**

Create `routers/epubs.py`:

```python
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
```

- [ ] **Step 4: Wire it into `main.py`**

Change the router import line (currently `from routers import novels, chapters, progress, settings, exports`):

```python
from routers import novels, chapters, progress, settings, exports, epubs
```

After `app.include_router(exports.router)` add:

```python
app.include_router(epubs.router)
```

In `lifespan`, after `export_worker.start_worker()` add:

```python
    import epub_library
    epub_library.start()
```

And immediately after `yield` (before the shutdown retention log line) add:

```python
    epub_library.stop()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests\test_epubs_api.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add routers/epubs.py main.py tests/test_epubs_api.py
git commit -m "feat: EPUB upload and cover endpoints; start folder sync with app"
```

---

### Task 5: UI delete removes the file (folder truth in both directions)

**Files:**
- Modify: `routers/novels.py` (`delete_novel`, around line 237)
- Modify: `tests/test_epubs_api.py` (append test)

**Interfaces:**
- Consumes: `epub_local.EPUB_DIR/filename_from_url/covers_dir`, `epub_library.forget`.
- Produces: `DELETE /api/novels/{id}` on an `epub://` novel also deletes the `.epub` file and its cached cover.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_epubs_api.py`:

```python
def test_ui_delete_removes_file_and_cover(client, tmp_path):
    body = _upload(client, tmp_path, cover=COVER_BYTES).json()
    assert (client.epub_dir / "My Book.epub").exists()
    assert (client.epub_dir / ".covers" / "My Book.jpg").exists()

    assert client.delete(f"/api/novels/{body['id']}").status_code == 204
    assert not (client.epub_dir / "My Book.epub").exists()
    assert not (client.epub_dir / ".covers" / "My Book.jpg").exists()
    assert all(n["id"] != body["id"] for n in client.get("/api/novels").json())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests\test_epubs_api.py::test_ui_delete_removes_file_and_cover -v`
Expected: FAIL — the novel row is deleted (204) but the `.epub` file still exists.

- [ ] **Step 3: Implement**

In `routers/novels.py`, add to the imports at the top:

```python
from pathlib import Path
```

In `delete_novel`, after the export-job cancellation block and before `db.delete(novel)`:

```python
    # EPUB books: the folder is the source of truth in both directions —
    # removing from the library also removes the file (and its cover)
    if novel.rr_url.startswith("epub://"):
        from scrapers import epub_local
        import epub_library
        filename = epub_local.filename_from_url(novel.rr_url)
        (epub_local.EPUB_DIR / filename).unlink(missing_ok=True)
        for cover in epub_local.covers_dir().glob(f"{Path(filename).stem}.*"):
            cover.unlink(missing_ok=True)
        epub_library.forget(filename)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests\test_epubs_api.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add routers/novels.py tests/test_epubs_api.py
git commit -m "feat: deleting an EPUB novel in the UI deletes its file"
```

---

### Task 6: Frontend — upload button and delete warning

**Files:**
- Modify: `frontend/index.html` (Add Novel modal, ~line 131)
- Modify: `frontend/app.js` (upload handler ~line 383; delete confirm ~line 303; listeners ~line 1708)

**Interfaces:**
- Consumes: `POST /api/epubs/upload` (Task 4); `NovelResponse.source == "epub"` (already emitted by the backend for `epub://` novels).
- Produces: user-visible upload path in the Add Novel modal; source-aware delete confirmation.

There is no JS test infrastructure in this repo — verification is manual (Step 4).

- [ ] **Step 1: Add the upload controls to the Add Novel modal**

In `frontend/index.html`, directly after the line

```html
            <input type="text" id="input-novel-url" placeholder="https://..." autocomplete="off">
```

insert:

```html
            <p class="hint" style="text-align:center; margin:8px 0;">— or —</p>
            <input type="file" id="input-epub-file" accept=".epub,application/epub+zip" style="display:none;">
            <button id="btn-upload-epub" class="secondary-btn" style="width:100%;">Upload an EPUB…</button>
```

- [ ] **Step 2: Add the upload handler and wire the listeners**

In `frontend/app.js`, after the `addNovel()` function (ends ~line 383), add:

```javascript
async function uploadEpub(file) {
    const errorEl = document.getElementById('add-error');
    const loadingEl = document.getElementById('add-loading');
    errorEl.style.display = 'none';
    loadingEl.textContent = 'Uploading EPUB…';
    loadingEl.style.display = 'block';
    try {
        const form = new FormData();
        form.append('file', file);
        const resp = await fetch('/api/epubs/upload', { method: 'POST', body: form });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: resp.statusText }));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        closeAddModal();
        showToast('Book added!');
        await loadLibrary();
    } catch (e) {
        errorEl.textContent = e.message;
        errorEl.style.display = 'block';
    } finally {
        loadingEl.style.display = 'none';
        loadingEl.textContent = 'Fetching novel info...';
    }
}
```

In the listener-registration section, after the existing add-novel listeners
(`document.getElementById('btn-add-confirm').addEventListener('click', addNovel);` ~line 1708), add:

```javascript
    document.getElementById('btn-upload-epub').addEventListener('click', () => {
        document.getElementById('input-epub-file').click();
    });
    document.getElementById('input-epub-file').addEventListener('change', (e) => {
        if (e.target.files.length) uploadEpub(e.target.files[0]);
        e.target.value = '';   // allow re-selecting the same file after an error
    });
```

- [ ] **Step 3: Source-aware delete confirmation**

In `frontend/app.js`, in the library-card delete handler (~line 302), replace:

```javascript
            const id = parseInt(btn.dataset.id);
            if (confirm('Remove this novel from your library?')) {
```

with:

```javascript
            const id = parseInt(btn.dataset.id);
            const novel = state.novels.find(n => n.id === id);
            const msg = novel?.source === 'epub'
                ? 'Remove this book? Its EPUB file will also be deleted from the EPUBs folder.'
                : 'Remove this novel from your library?';
            if (confirm(msg)) {
```

- [ ] **Step 4: Manual verification**

The server may already be running under `tray.py` with stale code — restart it
(see the repo memory: it runs `reload=False`). Then:

1. Start: `.venv\Scripts\python.exe main.py` and open `http://localhost:8000`.
2. Click **+ Add Novel** → **Upload an EPUB…** → pick any EPUB → the book
   appears in the library with its cover, `source` epub.
3. Copy a second EPUB into `EPUBs\` in Explorer → within ~10 s a reload of
   the page shows it.
4. Open the book, play a chapter — audio streams.
5. Delete the Explorer-copied file → book disappears from the library on reload.
6. Delete the uploaded book via its card's ✕ → confirm dialog mentions the
   file; the file vanishes from `EPUBs\`.

- [ ] **Step 5: Commit**

```bash
git add frontend/index.html frontend/app.js
git commit -m "feat: EPUB upload in Add Novel dialog; delete warns about file removal"
```

---

### Task 7: Full regression, README, .gitignore

**Files:**
- Modify: `.gitignore` (add the EPUBs folder)
- Modify: `README.md` (short "Adding EPUBs" blurb in "Everyday use")

- [ ] **Step 1: Ignore user books**

Append to `.gitignore` (skip any line that's already present):

```
EPUBs/
```

- [ ] **Step 2: README blurb**

In `README.md`, in the "Everyday use" section after the "Add a novel." paragraph, add:

```markdown
**Add an EPUB.** Drop an `.epub` file into the `EPUBs` folder next to the app
(it appears in your library within seconds), or click "+ Add Novel" →
"Upload an EPUB…" from your phone. The folder is the source of truth: delete
the file and the book leaves your library; delete the book in the app and the
file is removed too.
```

- [ ] **Step 3: Full test suite**

Run: `.venv\Scripts\python.exe -m pytest tests -v`
Expected: all tests pass (the pre-existing suite plus 17 new EPUB tests). If a pre-existing test fails, verify it also fails on the commit before this work (`git stash` / check out the base commit) before assuming this feature broke it.

- [ ] **Step 4: Commit**

```bash
git add .gitignore README.md
git commit -m "docs: EPUB library folder usage; ignore EPUBs folder"
```
