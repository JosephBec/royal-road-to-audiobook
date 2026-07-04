# Save to Plex M4B Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One-button export of a chapter range to a chaptered M4B that lands in the user's Plex audiobook folder and triggers a Plex API rescan, never degrading interactive streaming; plus a `--plex` delivery flag in the Ebook-to-Audiobook CLI.

**Architecture:** A DB-backed FIFO export job engine runs on the app's existing single TTS worker, synthesizing in ~600-word batches and yielding between batches to playback, prefetch debt, and favorites sync. Chapter WAVs accumulate in a per-job directory (survives restarts, enables retry-resume), then ffmpeg assembles the M4B with chapter markers and cover art. Plex refresh is section-level (Docker-safe) and non-fatal, with a distinct "Plex unreachable (is Docker running?)" message.

**Tech Stack:** FastAPI + SQLAlchemy/SQLite, Kokoro TTS (existing singleton pipeline), httpx, ffmpeg/ffprobe, soundfile/numpy, vanilla-JS frontend, pytest.

**Spec:** `docs/superpowers/specs/2026-07-04-save-to-plex-export-design.md` — read it first.

## Global Constraints

- Primary repo: `D:\Projects\royal-road-to-audiobook` (Tasks 1–12). Task 13 is in `D:\Projects\Ebook-to-Audiobook`.
- Python: each repo's own `.venv\Scripts\python.exe`. Run all commands from the repo root.
- File naming format is fixed: `"{sanitized_title} - Chapters {X} - {Y}.m4b"`. No author in the filename.
- Default audiobook dir (both repos): `E:\Plex\Audiobooks\Audiobooks`.
- Chapter numbers = `Chapter.order` (web app) / chapter index+1 (CLI).
- Export audio: mono 24000 Hz, PCM_16 intermediate WAVs, AAC 64k in M4B.
- Never reuse the streaming cache (`temp_audio/`) for exports; a job MAY reuse its own `export_jobs/<id>/` WAVs on retry.
- Export batch budget: 600 words, split on line boundaries.
- Priority: interactive synthesis > active-listener prefetch debt (heartbeat < 90s) > favorites sync > export.
- Plex refresh failures are non-fatal; connection errors/timeouts use the exact message: `"Plex is unreachable (is Docker running?) — the audiobook will appear after the next library scan."`
- Windows-illegal filename chars to strip: `<>:"/\|?*`, plus trailing dots/spaces.
- Commit after every task with the trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: Test infrastructure

**Files:**
- Create: `tests/__init__.py` (empty), `tests/conftest.py`
- Create: `requirements-dev.txt`
- Modify: `.gitignore` (add `export_jobs/` — used by later tasks)

**Interfaces:**
- Produces: pytest runnable via `.venv\Scripts\python.exe -m pytest`; env var `NOVEL_TTS_DB` points every test run at a throwaway SQLite file **before** `database` is imported (Task 2 makes `database.py` honor it).

- [ ] **Step 1: Write conftest and dev requirements**

`tests/conftest.py`:
```python
"""Test bootstrap: point the app at a throwaway SQLite DB before any
project module is imported (database.py reads NOVEL_TTS_DB at import time)."""
import os
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="noveltts_test_")
os.environ["NOVEL_TTS_DB"] = f"sqlite:///{_tmpdir}/test.db"
```

`requirements-dev.txt`:
```
pytest==8.3.2
```

Append to `.gitignore`:
```
export_jobs/
```

- [ ] **Step 2: Install and verify pytest collects**

Run: `.venv\Scripts\python.exe -m pip install -r requirements-dev.txt` then `.venv\Scripts\python.exe -m pytest --collect-only`
Expected: `no tests ran` / collected 0 items, exit without import errors.

- [ ] **Step 3: Commit**

```bash
git add tests/ requirements-dev.txt .gitignore
git commit -m "test: pytest infrastructure with isolated test DB"
```

---

### Task 2: Database — env-overridable URL, new columns, ExportJob model

**Files:**
- Modify: `database.py`
- Test: `tests/test_database.py`

**Interfaces:**
- Produces: `database.ExportJob` (columns: `id, novel_id, novel_title, author, start_order, end_order, voice, speed, status, chapters_done, chapters_total, detail, output_path, error, created_at, finished_at`); `Settings.audiobook_dir/.plex_url/.plex_token/.plex_section_id`; `Chapter.text`; `DATABASE_URL` honors env `NOVEL_TTS_DB`.
- Status values (string): `queued | running | completed | failed | interrupted | canceled`.

- [ ] **Step 1: Write the failing test**

`tests/test_database.py`:
```python
from sqlalchemy import inspect as sa_inspect


def test_new_schema_elements():
    import database
    database.init_db()
    insp = sa_inspect(database.engine)

    settings_cols = {c["name"] for c in insp.get_columns("settings")}
    assert {"audiobook_dir", "plex_url", "plex_token", "plex_section_id"} <= settings_cols

    chapter_cols = {c["name"] for c in insp.get_columns("chapters")}
    assert "text" in chapter_cols

    job_cols = {c["name"] for c in insp.get_columns("export_jobs")}
    assert {"novel_id", "novel_title", "author", "start_order", "end_order",
            "voice", "speed", "status", "chapters_done", "chapters_total",
            "detail", "output_path", "error", "created_at", "finished_at"} <= job_cols

    db = database.SessionLocal()
    try:
        s = db.query(database.Settings).first()
        assert s.audiobook_dir == r"E:\Plex\Audiobooks\Audiobooks"
        assert s.plex_url == "" and s.plex_token == "" and s.plex_section_id == ""
    finally:
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_database.py -v`
Expected: FAIL (missing columns / no `export_jobs` table).

- [ ] **Step 3: Implement**

In `database.py`:

Add `import os` at the top and replace the URL constant:
```python
DATABASE_URL = os.environ.get("NOVEL_TTS_DB", "sqlite:///./data.db")
```

Add to the `Settings` model:
```python
    audiobook_dir = Column(Text, nullable=False, default=r"E:\Plex\Audiobooks\Audiobooks")
    plex_url = Column(Text, nullable=False, default="")
    plex_token = Column(Text, nullable=False, default="")
    plex_section_id = Column(Text, nullable=False, default="")
```

Add to the `Chapter` model:
```python
    text = Column(Text, nullable=True)  # scraped chapter text cache (scrape once, ever)
```

Add the `ExportJob` model after `Settings`:
```python
class ExportJob(Base):
    __tablename__ = "export_jobs"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), nullable=False)
    novel_title = Column(Text, nullable=False)   # snapshot: job survives novel edits
    author = Column(Text, default="Unknown")
    start_order = Column(Integer, nullable=False)
    end_order = Column(Integer, nullable=False)
    voice = Column(String, nullable=False)
    speed = Column(Float, nullable=False)
    status = Column(String, nullable=False, default="queued")
    chapters_done = Column(Integer, default=0)
    chapters_total = Column(Integer, default=0)
    detail = Column(Text, default="")
    output_path = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime, nullable=True)
```

Generalize `_migrate_schema` to multiple tables (replace the whole function; `export_jobs` is a new table so `create_all` handles it):
```python
def _migrate_schema():
    """Add columns introduced after initial release (SQLite has no Alembic here)."""
    inspector = sa_inspect(engine)
    table_columns = {
        "novels": {
            "voice": "TEXT",
            "speed": "FLOAT",
            "auto_play": "BOOLEAN",
            "chapter_sort": "TEXT",
            "favorite": "BOOLEAN NOT NULL DEFAULT 0",
            "sort_order": "INTEGER",
        },
        "chapters": {
            "text": "TEXT",
        },
        "settings": {
            "audiobook_dir": "TEXT NOT NULL DEFAULT 'E:\\Plex\\Audiobooks\\Audiobooks'",
            "plex_url": "TEXT NOT NULL DEFAULT ''",
            "plex_token": "TEXT NOT NULL DEFAULT ''",
            "plex_section_id": "TEXT NOT NULL DEFAULT ''",
        },
    }
    with engine.begin() as conn:
        for table, new_columns in table_columns.items():
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl_type in new_columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_database.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add database.py tests/test_database.py
git commit -m "feat: export_jobs table, chapter text cache, Plex/export settings columns"
```

---

### Task 3: Chapter text cache in playback routes

**Files:**
- Modify: `routers/chapters.py` (both `stream_chapter` and `start_synthesis`)
- Test: `tests/test_text_cache.py`

**Interfaces:**
- Consumes: `Chapter.text` (Task 2).
- Produces: playback scrapes populate `Chapter.text`; cached text short-circuits scraping. Convention: `Chapter.text` stores the **raw scraped text** (no title prefix); the `f"{chapter.title}\n\n{text}"` announcement is added at synthesis call sites only.

- [ ] **Step 1: Write the failing test**

`tests/test_text_cache.py`:
```python
"""The scrape-or-cache decision is extracted into a helper so it is testable
without HTTP: routers.chapters.get_chapter_text(chapter, db)."""
import asyncio

import pytest


@pytest.fixture()
def db_novel_chapter():
    import database
    database.init_db()
    db = database.SessionLocal()
    novel = database.Novel(title="T", rr_url="https://www.royalroad.com/fiction/999001/t")
    db.add(novel); db.commit()
    ch = database.Chapter(novel_id=novel.id, title="Ch 1", order=1,
                          rr_url="https://www.royalroad.com/fiction/999001/t/chapter/1/c1")
    db.add(ch); db.commit()
    yield db, ch
    db.query(database.Chapter).filter_by(id=ch.id).delete()
    db.query(database.Novel).filter_by(id=novel.id).delete()
    db.commit(); db.close()


def test_cached_text_skips_scraping(db_novel_chapter, monkeypatch):
    db, ch = db_novel_chapter
    ch.text = "cached body"; db.commit()
    from routers import chapters as chapters_router

    def boom(url):  # any scraper resolution means we tried to scrape
        raise AssertionError("should not scrape when text is cached")
    monkeypatch.setattr(chapters_router, "_scraper_for", boom)

    text = asyncio.run(chapters_router.get_chapter_text(ch, db))
    assert text == "cached body"


def test_scrape_populates_cache(db_novel_chapter, monkeypatch):
    db, ch = db_novel_chapter
    from routers import chapters as chapters_router

    class FakeScraper:
        async def scrape_chapter_text(self, url):
            return "fresh body"
    monkeypatch.setattr(chapters_router, "_scraper_for", lambda url: FakeScraper())

    text = asyncio.run(chapters_router.get_chapter_text(ch, db))
    assert text == "fresh body"
    db.refresh(ch)
    assert ch.text == "fresh body"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_text_cache.py -v`
Expected: FAIL — `get_chapter_text` does not exist.

- [ ] **Step 3: Implement**

In `routers/chapters.py`, add after `_scraper_for`:
```python
async def get_chapter_text(chapter: Chapter, db: Session) -> str:
    """Return chapter body text, scraping and caching it on first use.

    Stored text is the raw scraped body (no title announcement).
    """
    if chapter.text:
        return chapter.text
    text = await _scraper_for(chapter.rr_url).scrape_chapter_text(chapter.rr_url)
    chapter.text = text
    chapter.word_count = len(text.split())
    chapter.fetched_at = datetime.now(timezone.utc)
    db.commit()
    return text
```

In `stream_chapter`, replace the scrape block (the `try/except` around `scrape_chapter_text` plus the word-count update) with:
```python
    try:
        text = await get_chapter_text(chapter, db)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to scrape chapter text: %s", e)
        raise HTTPException(status_code=502, detail=f"Failed to fetch chapter text: {e}")
    text = f"{chapter.title}\n\n{text}"
```

In `start_synthesis`, replace its scrape block (including the word-count/`fetched_at` update lines) the same way:
```python
    try:
        text = await get_chapter_text(chapter, db)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch chapter text: {e}")
    # Announce the chapter title at the start of the audio
    text = f"{chapter.title}\n\n{text}"
```

Also update the prefetch loop in `_after_synthesis` to use/populate the cache. Replace the body of the `for pf_id, pf_url, pf_title in prefetch_targets:` loop's scrape line — first extend `prefetch_targets` to carry ids only and reload from DB (text cache lives on the row):
```python
    async def _after_synthesis():
        """Pre-download the next 3 chapters, then apply retention cleanup."""
        for pf_id, pf_url, pf_title in prefetch_targets:
            if temp_path_for_chapter(pf_id).exists():
                continue
            db2 = SessionLocal()
            try:
                pf_chapter = db2.query(Chapter).filter(Chapter.id == pf_id).first()
                if not pf_chapter:
                    continue
                pf_text = await get_chapter_text(pf_chapter, db2)
                await synthesize_chapter_to_file(pf_id, f"{pf_title}\n\n{pf_text}", voice, 1.0)
            except Exception as e:
                logger.warning("Prefetch failed for chapter %s: %s", pf_id, e)
            finally:
                db2.close()
        db2 = SessionLocal()
        try:
            forever, expiring = retention_policy(db2)
        finally:
            db2.close()
        cleanup_temp_files(keep_ids | forever, expiring)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_text_cache.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add routers/chapters.py tests/test_text_cache.py
git commit -m "feat: chapter text cache - scrape once, reuse for playback/prefetch/export"
```

---

### Task 4: `m4b.py` — sanitization, naming, ffmetadata (pure functions)

**Files:**
- Create: `m4b.py`
- Test: `tests/test_m4b_naming.py`

**Interfaces:**
- Produces:
  - `sanitize_title(title: str) -> str`
  - `export_basename(title: str, start: int, end: int) -> str` → `"{title} - Chapters {start} - {end}"`
  - `ffmetadata_content(chapters: list[tuple[str, float]]) -> str` — `(title, duration_seconds)` per chapter, in order.

- [ ] **Step 1: Write the failing test**

`tests/test_m4b_naming.py`:
```python
from m4b import sanitize_title, export_basename, ffmetadata_content


def test_sanitize_strips_windows_illegal_chars():
    assert sanitize_title('He said: "Go/No*Go?" <now>') == "He said Go No Go now"


def test_sanitize_trims_trailing_dots_and_spaces():
    assert sanitize_title("Book Vol. 2. ") == "Book Vol. 2"


def test_sanitize_empty_falls_back():
    assert sanitize_title("???") == "Untitled"


def test_export_basename():
    assert export_basename("The Hundred Reigns", 33, 74) == "The Hundred Reigns - Chapters 33 - 74"


def test_ffmetadata_chapters_and_escaping():
    content = ffmetadata_content([("Ch 1; a=b", 2.0), ("Ch 2", 1.5)])
    assert content.startswith(";FFMETADATA1")
    assert "START=0" in content and "END=2000" in content
    assert "START=2000" in content and "END=3500" in content
    assert r"title=Ch 1\; a\=b" in content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_m4b_naming.py -v`
Expected: FAIL — `No module named 'm4b'`.

- [ ] **Step 3: Implement**

`m4b.py`:
```python
"""M4B audiobook assembly: naming, chapter metadata, and ffmpeg encoding.

Adapted from Ebook-to-Audiobook's audiobook_builder.py, reworked to read
chapter audio from WAV files on disk instead of holding arrays in RAM.
"""

import logging
import re
import shutil
import subprocess
from pathlib import Path

import soundfile as sf

logger = logging.getLogger(__name__)

_INVALID = set('<>:"/\\|?*')


def sanitize_title(title: str) -> str:
    """Make a string safe as a Windows filename component."""
    cleaned = "".join(" " if (c in _INVALID or ord(c) < 32) else c for c in title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(" .")
    return cleaned or "Untitled"


def export_basename(title: str, start: int, end: int) -> str:
    """Fixed naming format: 'Title - Chapters X - Y' (no author, not configurable)."""
    return f"{sanitize_title(title)} - Chapters {start} - {end}"


def _ffescape(s: str) -> str:
    """Escape ffmetadata special characters (=, ;, #, \\, newline)."""
    return re.sub(r"([=;#\\\n])", r"\\\1", s)


def ffmetadata_content(chapters: list) -> str:
    """Build an FFMETADATA1 file body from (title, duration_seconds) tuples."""
    lines = [";FFMETADATA1"]
    t_ms = 0
    for title, duration in chapters:
        end_ms = t_ms + int(duration * 1000)
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={t_ms}",
            f"END={end_ms}",
            f"title={_ffescape(title)}",
        ]
        t_ms = end_ms
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_m4b_naming.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add m4b.py tests/test_m4b_naming.py
git commit -m "feat: m4b naming/sanitization/ffmetadata helpers"
```

---

### Task 5: `m4b.py` — `assemble_m4b` (ffmpeg concat + encode)

**Files:**
- Modify: `m4b.py`
- Test: `tests/test_m4b_assemble.py`

**Interfaces:**
- Produces: `assemble_m4b(chapter_wavs: list[tuple[str, Path]], out_path: Path, *, book_title: str, author: str, cover_bytes: bytes | None = None, cover_ext: str = "jpg", bitrate: str = "64k") -> Path` — blocking; callers on the event loop use `asyncio.to_thread`. `chapter_wavs` is `(chapter_title, wav_path)` in playback order; raises `RuntimeError` on ffmpeg failure.

- [ ] **Step 1: Write the failing test**

`tests/test_m4b_assemble.py`:
```python
import json
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from m4b import assemble_m4b


def _sine_wav(path: Path, seconds: float, freq: float):
    t = np.linspace(0, seconds, int(24000 * seconds), endpoint=False)
    sf.write(str(path), (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32),
             24000, subtype="PCM_16")


def test_assemble_produces_chaptered_m4b(tmp_path):
    w1, w2 = tmp_path / "c1.wav", tmp_path / "c2.wav"
    _sine_wav(w1, 1.0, 440); _sine_wav(w2, 1.5, 660)
    out = tmp_path / "book.m4b"

    result = assemble_m4b([("Ch One", w1), ("Ch Two", w2)], out,
                          book_title="Test Book", author="Tester")

    assert result == out and out.exists()
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_chapters", "-show_format", "-of", "json", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    data = json.loads(probe.stdout)
    titles = [c["tags"]["title"] for c in data["chapters"]]
    assert titles == ["Ch One", "Ch Two"]
    assert abs(float(data["format"]["duration"]) - 2.5) < 0.2
    assert data["format"]["tags"].get("title") == "Test Book"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_m4b_assemble.py -v`
Expected: FAIL — `cannot import name 'assemble_m4b'`.

- [ ] **Step 3: Implement**

Append to `m4b.py`:
```python
def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise RuntimeError("ffmpeg not found on PATH — required for M4B export.")
    return path


def _run(cmd: list, timeout: int, what: str):
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{what} failed: {result.stderr[-2000:]}")


def assemble_m4b(
    chapter_wavs: list,
    out_path: Path,
    *,
    book_title: str,
    author: str,
    cover_bytes: bytes | None = None,
    cover_ext: str = "jpg",
    bitrate: str = "64k",
) -> Path:
    """Concatenate chapter WAVs and encode a chaptered M4B. Blocking."""
    ffmpeg = _ffmpeg()
    out_path = Path(out_path)
    workdir = out_path.parent

    durations = [(title, sf.info(str(wav)).duration) for title, wav in chapter_wavs]
    total = sum(d for _, d in durations)

    concat_list = workdir / "concat_list.txt"
    concat_list.write_text(
        "".join(f"file '{str(wav).replace(chr(39), chr(39) + chr(92) + chr(39) * 2)}'\n"
                for _, wav in chapter_wavs),
        encoding="utf-8")

    metadata_path = workdir / "metadata.txt"
    metadata_path.write_text(ffmetadata_content(durations), encoding="utf-8")

    combined = workdir / "combined.wav"
    _run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
          "-c", "copy", str(combined)],
         timeout=max(600, int(total * 0.5) + 600), what="WAV concat")

    cover_path = None
    if cover_bytes:
        cover_path = workdir / f"cover.{cover_ext}"
        cover_path.write_bytes(cover_bytes)

    cmd = [ffmpeg, "-y", "-i", str(combined), "-i", str(metadata_path)]
    if cover_path:
        cmd += ["-i", str(cover_path),
                "-map", "0:a", "-map", "2:v", "-map_metadata", "1",
                "-c:a", "aac", "-b:a", bitrate, "-ar", "24000", "-ac", "1",
                "-c:v", "copy", "-disposition:v", "attached_pic"]
    else:
        cmd += ["-map_metadata", "1",
                "-c:a", "aac", "-b:a", bitrate, "-ar", "24000", "-ac", "1"]
    cmd += ["-metadata", f"title={book_title}",
            "-metadata", f"artist={author}",
            "-metadata", f"album={book_title}",
            "-metadata", "genre=Audiobook",
            "-f", "mp4", str(out_path)]
    _run(cmd, timeout=max(1200, int(total) + 1200), what="M4B encode")

    combined.unlink(missing_ok=True)
    concat_list.unlink(missing_ok=True)
    metadata_path.unlink(missing_ok=True)
    if cover_path:
        cover_path.unlink(missing_ok=True)

    logger.info("M4B assembled: %s (%.1f min)", out_path, total / 60)
    return out_path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_m4b_assemble.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add m4b.py tests/test_m4b_assemble.py
git commit -m "feat: file-based M4B assembly with chapter markers and cover art"
```

---

### Task 6: Batch splitting + shared-worker batch synthesis

**Files:**
- Create: `textbatch.py`
- Modify: `tts.py`
- Test: `tests/test_textbatch.py`

**Interfaces:**
- Produces:
  - `textbatch.split_batches(text: str, max_words: int = 600) -> list[str]` — splits on line boundaries; a single over-budget line stays intact as its own batch; joining batches with `"\n"` loses no non-empty line.
  - `tts.synthesize_batch(text: str, voice: str, speed: float) -> list[np.ndarray]` (async) — one executor job on the shared worker; the seam a future parallel export lane would replace.

- [ ] **Step 1: Write the failing test**

`tests/test_textbatch.py`:
```python
from textbatch import split_batches


def test_short_text_is_one_batch():
    assert split_batches("hello world\nsecond line") == ["hello world\nsecond line"]


def test_splits_on_line_boundaries_at_budget():
    lines = [f"word {' x' * 9}" for _ in range(100)]  # 10 words per line
    batches = split_batches("\n".join(lines), max_words=25)
    assert all(len(b.split()) <= 25 for b in batches)
    assert sum(len(b.split("\n")) for b in batches) == 100


def test_single_huge_line_stays_intact():
    huge = "w " * 1000
    batches = split_batches(huge.strip(), max_words=600)
    assert len(batches) == 1


def test_no_content_lost():
    text = "a\n\nb\nc"
    joined = "\n".join(split_batches(text, max_words=1))
    assert [l for l in joined.split("\n") if l.strip()] == ["a", "b", "c"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_textbatch.py -v`
Expected: FAIL — `No module named 'textbatch'`.

- [ ] **Step 3: Implement**

`textbatch.py`:
```python
"""Split chapter text into synthesis batches for the export worker.

Batches are the yield granularity of exports: between batches the worker
re-checks whether playback/prefetch/favorites need the TTS worker.
"""


def split_batches(text: str, max_words: int = 600) -> list[str]:
    """Group non-empty lines into batches of at most max_words words.

    A single line longer than the budget is emitted alone rather than split,
    so Kokoro still sees intact paragraphs.
    """
    lines = [l for l in text.split("\n") if l.strip()]
    batches: list[str] = []
    current: list[str] = []
    count = 0
    for line in lines:
        words = len(line.split())
        if current and count + words > max_words:
            batches.append("\n".join(current))
            current, count = [], 0
        current.append(line)
        count += words
    if current:
        batches.append("\n".join(current))
    return batches
```

Append to `tts.py` (after `synthesize_chapter_to_file`):
```python
async def synthesize_batch(text: str, voice: str, speed: float) -> list[np.ndarray]:
    """Synthesize one export batch on the shared TTS worker.

    Deliberately one small executor job: exports call this per ~600-word
    batch and yield between calls, keeping worst-case playback latency to
    a single batch. (A future parallel export lane replaces this seam.)
    """
    pipeline = await get_pipeline(voice)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor, _synthesize_text_blocking, pipeline, text, voice, speed
    )
```

Also add `import asyncio` to `tts.py`'s imports if not present (it is already imported).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_textbatch.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add textbatch.py tts.py tests/test_textbatch.py
git commit -m "feat: export batch splitting and shared-worker batch synthesis"
```

---

### Task 7: `plex.py` client with unreachable detection

**Files:**
- Create: `plex.py`
- Test: `tests/test_plex.py`

**Interfaces:**
- Produces:
  - `PLEX_UNREACHABLE_MSG = "Plex is unreachable (is Docker running?) — the audiobook will appear after the next library scan."`
  - `class PlexUnreachable(Exception)`
  - `async list_libraries(url: str, token: str, *, transport=None) -> list[dict]` — `[{"id": str, "title": str, "type": str}]`
  - `async trigger_refresh(url: str, token: str, section_id: str, *, transport=None) -> None`
  - Connection errors/timeouts raise `PlexUnreachable`; HTTP errors (401 etc.) raise `httpx.HTTPStatusError`.

- [ ] **Step 1: Write the failing test**

`tests/test_plex.py`:
```python
import asyncio

import httpx
import pytest

import plex


def _transport(handler):
    return httpx.MockTransport(handler)


def test_list_libraries_parses_sections():
    def handler(request):
        assert request.url.path == "/library/sections"
        assert request.url.params["X-Plex-Token"] == "tok"
        return httpx.Response(200, json={"MediaContainer": {"Directory": [
            {"key": "5", "title": "Audiobooks", "type": "artist"},
            {"key": "1", "title": "Movies", "type": "movie"},
        ]}})

    libs = asyncio.run(plex.list_libraries("http://plex:32400", "tok",
                                           transport=_transport(handler)))
    assert libs == [{"id": "5", "title": "Audiobooks", "type": "artist"},
                    {"id": "1", "title": "Movies", "type": "movie"}]


def test_trigger_refresh_hits_section_endpoint():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200)

    asyncio.run(plex.trigger_refresh("http://plex:32400/", "tok", "5",
                                     transport=_transport(handler)))
    assert calls == ["/library/sections/5/refresh"]


def test_connection_error_raises_plex_unreachable():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(plex.PlexUnreachable):
        asyncio.run(plex.trigger_refresh("http://plex:32400", "tok", "5",
                                         transport=_transport(handler)))


def test_http_error_is_not_unreachable():
    def handler(request):
        return httpx.Response(401)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(plex.list_libraries("http://plex:32400", "tok",
                                        transport=_transport(handler)))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_plex.py -v`
Expected: FAIL — `No module named 'plex'`.

- [ ] **Step 3: Implement**

`plex.py`:
```python
"""Minimal Plex Media Server client: list libraries, trigger section refresh.

Section-level refresh only (no path parameter): Plex runs in Docker, so the
container's filesystem paths differ from this machine's Windows paths.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

PLEX_UNREACHABLE_MSG = (
    "Plex is unreachable (is Docker running?) — "
    "the audiobook will appear after the next library scan."
)


class PlexUnreachable(Exception):
    """Plex did not answer at the network level (Docker engine down, wrong host)."""


async def _get(url: str, token: str, path: str, *, transport=None) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport) as client:
            resp = await client.get(
                f"{url.rstrip('/')}{path}",
                params={"X-Plex-Token": token},
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp
    except httpx.TransportError as e:
        # ConnectError, timeouts, DNS failures — the server never answered.
        raise PlexUnreachable(PLEX_UNREACHABLE_MSG) from e


async def list_libraries(url: str, token: str, *, transport=None) -> list[dict]:
    resp = await _get(url, token, "/library/sections", transport=transport)
    dirs = resp.json().get("MediaContainer", {}).get("Directory", [])
    return [{"id": str(d.get("key")), "title": d.get("title"), "type": d.get("type")}
            for d in dirs]


async def trigger_refresh(url: str, token: str, section_id: str, *, transport=None) -> None:
    await _get(url, token, f"/library/sections/{section_id}/refresh", transport=transport)
    logger.info("Plex section %s refresh triggered", section_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_plex.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add plex.py tests/test_plex.py
git commit -m "feat: Plex client with section refresh and unreachable detection"
```

---

### Task 8: Export priority gate

**Files:**
- Create: `export_worker.py` (gate portion)
- Modify: `library_sync.py` (add `is_running()`)
- Test: `tests/test_export_gate.py`

**Interfaces:**
- Consumes: `tts.interactive_busy()`, `tts.temp_path_for_chapter(id)`, `database.Progress/Chapter`, `library_sync`.
- Produces:
  - `library_sync.is_running() -> bool`
  - `export_worker.HEARTBEAT_WINDOW = 90`
  - `export_worker._active_listener_debt(db) -> bool`
  - `export_worker._export_may_proceed(db) -> bool` — True only when interactive idle AND no listener debt AND favorites sync idle.
  - `export_worker.ExportCanceled(Exception)`; `export_worker._cancel_requested: set[int]`
  - `async export_worker._wait_for_export_turn(job_id: int)` — polls every 2s; raises `ExportCanceled` if `job_id` is in `_cancel_requested`.

- [ ] **Step 1: Write the failing test**

`tests/test_export_gate.py`:
```python
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def fresh_db():
    import database
    database.init_db()
    db = database.SessionLocal()
    yield db, database
    db.query(database.Progress).delete()
    db.query(database.Chapter).delete()
    db.query(database.Novel).delete()
    db.commit(); db.close()


def _novel_with_chapters(db, database, n=5, url_seed="gate1"):
    novel = database.Novel(title="G", rr_url=f"https://www.royalroad.com/fiction/888/{url_seed}")
    db.add(novel); db.commit()
    chapters = []
    for i in range(1, n + 1):
        ch = database.Chapter(novel_id=novel.id, title=f"C{i}", order=i,
                              rr_url=f"https://www.royalroad.com/fiction/888/{url_seed}/chapter/{i}/c")
        db.add(ch); chapters.append(ch)
    db.commit()
    return novel, chapters


def test_no_listeners_no_debt(fresh_db):
    db, database = fresh_db
    import export_worker
    assert export_worker._active_listener_debt(db) is False


def test_recent_listener_with_cold_cache_is_debt(fresh_db):
    db, database = fresh_db
    novel, chapters = _novel_with_chapters(db, database, url_seed="gate2")
    db.add(database.Progress(novel_id=novel.id, chapter_id=chapters[0].id,
                             updated_at=datetime.now(timezone.utc)))
    db.commit()
    import export_worker
    assert export_worker._active_listener_debt(db) is True


def test_recent_listener_with_warm_cache_is_not_debt(fresh_db, tmp_path, monkeypatch):
    db, database = fresh_db
    novel, chapters = _novel_with_chapters(db, database, url_seed="gate3")
    db.add(database.Progress(novel_id=novel.id, chapter_id=chapters[0].id,
                             updated_at=datetime.now(timezone.utc)))
    db.commit()
    import export_worker, tts
    monkeypatch.setattr(tts, "TEMP_DIR", tmp_path)
    for ch in chapters[:4]:  # current + next 3
        tts.temp_path_for_chapter(ch.id).write_bytes(b"x")
    assert export_worker._active_listener_debt(db) is False


def test_stale_listener_is_not_debt(fresh_db):
    db, database = fresh_db
    novel, chapters = _novel_with_chapters(db, database, url_seed="gate4")
    db.add(database.Progress(novel_id=novel.id, chapter_id=chapters[0].id,
                             updated_at=datetime.now(timezone.utc) - timedelta(seconds=600)))
    db.commit()
    import export_worker
    assert export_worker._active_listener_debt(db) is False


def test_may_proceed_blocks_on_interactive(fresh_db, monkeypatch):
    db, database = fresh_db
    import export_worker, tts
    monkeypatch.setattr(tts, "interactive_busy", lambda: True)
    assert export_worker._export_may_proceed(db) is False
    monkeypatch.setattr(tts, "interactive_busy", lambda: False)
    assert export_worker._export_may_proceed(db) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_export_gate.py -v`
Expected: FAIL — `No module named 'export_worker'`.

- [ ] **Step 3: Implement**

Add to `library_sync.py` (after `start_refresh`):
```python
def is_running() -> bool:
    """True while a favorites sync pass is active (exports yield to it)."""
    return _task is not None and not _task.done()
```

Create `export_worker.py`:
```python
"""Save-to-Plex export job engine.

Jobs synthesize chapter ranges to M4B on the shared TTS worker at strictly
lowest priority: between every ~600-word batch the worker yields until
playback is idle, active listeners' prefetch is warm, and favorites sync
is done. Artifacts live in export_jobs/<id>/ (survives restarts; retry
resumes by skipping finished chapter WAVs).
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from database import SessionLocal, Chapter, Progress
import library_sync
import tts

logger = logging.getLogger(__name__)

EXPORT_DIR = Path("./export_jobs")
HEARTBEAT_WINDOW = 90  # seconds: progress update newer than this = active listener
PREFETCH_DEPTH = 3

_cancel_requested: set[int] = set()


class ExportCanceled(Exception):
    """Raised inside a job when the user cancels it."""


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _active_listener_debt(db) -> bool:
    """True if any recently-active listener lacks current+next-3 cached audio.

    The existing after-play prefetch chain fills these files; the export
    merely stays off the worker until they exist.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=HEARTBEAT_WINDOW)
    recent = db.query(Progress).filter(Progress.chapter_id.isnot(None)).all()
    for prog in recent:
        if prog.updated_at is None or _as_utc(prog.updated_at) < cutoff:
            continue
        current = db.query(Chapter).filter(Chapter.id == prog.chapter_id).first()
        if current is None:
            continue
        needed = [current.id] + [
            c.id for c in db.query(Chapter)
            .filter(Chapter.novel_id == current.novel_id, Chapter.order > current.order)
            .order_by(Chapter.order).limit(PREFETCH_DEPTH).all()
        ]
        if any(not tts.temp_path_for_chapter(cid).exists() for cid in needed):
            return True
    return False


def _export_may_proceed(db) -> bool:
    """Export priority gate: everything else outranks exports."""
    return (not tts.interactive_busy()
            and not _active_listener_debt(db)
            and not library_sync.is_running())


async def _wait_for_export_turn(job_id: int):
    """Block until the export may use the TTS worker; raise on cancel."""
    waited = False
    while True:
        if job_id in _cancel_requested:
            raise ExportCanceled()
        db = SessionLocal()
        try:
            ok = _export_may_proceed(db)
        finally:
            db.close()
        if ok:
            return
        if not waited:
            _update_job(job_id, detail="waiting for playback to idle")
            waited = True
        await asyncio.sleep(2)


def _update_job(job_id: int, **fields):
    """Short-lived-session job row update (worker holds no long sessions)."""
    from database import ExportJob
    db = SessionLocal()
    try:
        job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
        if job:
            for k, v in fields.items():
                setattr(job, k, v)
            db.commit()
    finally:
        db.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_export_gate.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add export_worker.py library_sync.py tests/test_export_gate.py
git commit -m "feat: export priority gate - playback, prefetch debt, favorites sync all outrank exports"
```

---

### Task 9: Export job runner (synthesis, resume, assembly, delivery)

**Files:**
- Modify: `export_worker.py`, `main.py`
- Test: `tests/test_export_worker.py`

**Interfaces:**
- Consumes: `textbatch.split_batches`, `tts.synthesize_batch`, `m4b.assemble_m4b/export_basename`, `plex.trigger_refresh/PlexUnreachable/PLEX_UNREACHABLE_MSG`, Task 8 gate, `routers.chapters.get_chapter_text` is NOT used (worker has its own scrape-with-retry to avoid importing a router).
- Produces:
  - `enqueue(job_id: int)` / `request_cancel(job_id: int)`
  - `start_worker()` — creates the singleton `asyncio` worker task (call from lifespan)
  - `startup_recover()` — marks `running`→`interrupted`, re-enqueues `queued` jobs (call from lifespan before `start_worker`)
  - `async _run_job(job_id: int)` — full pipeline; also `async _get_text(chapter_id: int) -> str` (cache→scrape×3→store) and `async _synthesize_chapter_wav(job_id, chapter_id, title, voice, speed, wav_path)`.
  - Chapter WAV naming inside a job dir: `chapter_{order:05d}.wav`.

- [ ] **Step 1: Write the failing test**

`tests/test_export_worker.py`:
```python
"""Integration test of _run_job with fake TTS and fake scraping — no GPU,
no network, no real ffmpeg encode (assemble_m4b is monkeypatched)."""
import asyncio
from pathlib import Path

import numpy as np
import pytest


@pytest.fixture()
def job_env(tmp_path, monkeypatch):
    import database, export_worker, tts
    database.init_db()
    db = database.SessionLocal()

    novel = database.Novel(title="Job Novel", author="A",
                           rr_url="https://www.royalroad.com/fiction/777/job")
    db.add(novel); db.commit()
    chapters = []
    for i in range(1, 4):
        ch = database.Chapter(novel_id=novel.id, title=f"C{i}", order=i, text=f"body {i}",
                              rr_url=f"https://www.royalroad.com/fiction/777/job/chapter/{i}/c")
        db.add(ch); chapters.append(ch)
    db.commit()

    job = database.ExportJob(novel_id=novel.id, novel_title=novel.title, author="A",
                             start_order=1, end_order=3, voice="af_heart", speed=1.0,
                             chapters_total=3)
    db.add(job); db.commit()

    monkeypatch.setattr(export_worker, "EXPORT_DIR", tmp_path / "jobs")
    monkeypatch.setattr(export_worker, "_export_may_proceed", lambda _db: True)

    async def fake_batch(text, voice, speed):
        return [np.zeros(2400, dtype=np.float32)]
    monkeypatch.setattr(tts, "synthesize_batch", fake_batch)

    assembled = {}
    def fake_assemble(chapter_wavs, out_path, **kw):
        assembled["chapters"] = [t for t, _ in chapter_wavs]
        assembled["kwargs"] = kw
        Path(out_path).write_bytes(b"m4b")
        return Path(out_path)
    import m4b
    monkeypatch.setattr(m4b, "assemble_m4b", fake_assemble)

    settings = db.query(database.Settings).first()
    settings.audiobook_dir = str(tmp_path / "plexlib")
    settings.plex_url = ""  # not configured: refresh skipped with note
    db.commit()

    yield db, database, export_worker, job, assembled
    db.query(database.ExportJob).delete()
    db.query(database.Chapter).delete()
    db.query(database.Progress).delete()
    db.query(database.Novel).delete()
    db.commit(); db.close()


def test_run_job_produces_named_m4b(job_env, tmp_path):
    db, database, export_worker, job, assembled = job_env
    asyncio.run(export_worker._run_job(job.id))

    db.expire_all()
    fresh = db.query(database.ExportJob).filter_by(id=job.id).first()
    assert fresh.status == "completed"
    assert fresh.chapters_done == 3
    out = Path(fresh.output_path)
    assert out.name == "Job Novel - Chapters 1 - 3.m4b"
    assert out.exists()
    assert assembled["chapters"] == ["C1", "C2", "C3"]
    assert not (export_worker.EXPORT_DIR / str(job.id)).exists()  # cleaned on success


def test_retry_skips_existing_chapter_wavs(job_env, monkeypatch):
    db, database, export_worker, job, assembled = job_env
    import tts
    calls = {"n": 0}

    async def counting_batch(text, voice, speed):
        calls["n"] += 1
        import numpy as np
        return [np.zeros(2400, dtype=np.float32)]
    monkeypatch.setattr(tts, "synthesize_batch", counting_batch)

    job_dir = export_worker.EXPORT_DIR / str(job.id)
    job_dir.mkdir(parents=True)
    import soundfile as sf
    import numpy as np
    sf.write(str(job_dir / "chapter_00001.wav"),
             np.zeros(2400, dtype=np.float32), 24000, subtype="PCM_16")

    asyncio.run(export_worker._run_job(job.id))
    assert calls["n"] == 2  # chapters 2 and 3 only


def test_cancel_marks_job_canceled(job_env, monkeypatch):
    db, database, export_worker, job, assembled = job_env
    import tts

    async def cancel_then_batch(text, voice, speed):
        export_worker.request_cancel(job.id)
        import numpy as np
        return [np.zeros(2400, dtype=np.float32)]
    monkeypatch.setattr(tts, "synthesize_batch", cancel_then_batch)

    asyncio.run(export_worker._run_job(job.id))
    db.expire_all()
    fresh = db.query(database.ExportJob).filter_by(id=job.id).first()
    assert fresh.status == "canceled"
    assert (export_worker.EXPORT_DIR / str(job.id)).exists()  # kept for retry
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_export_worker.py -v`
Expected: FAIL — `_run_job` / `request_cancel` not defined.

- [ ] **Step 3: Implement**

Append to `export_worker.py`:
```python
import shutil

import httpx
import numpy as np
import soundfile as sf

import m4b
import plex
import textbatch
from database import ExportJob, Novel, Settings
from scrapers import get_scraper_for_url

_queue: asyncio.Queue | None = None
_worker_task: asyncio.Task | None = None


def request_cancel(job_id: int):
    _cancel_requested.add(job_id)


def enqueue(job_id: int):
    assert _queue is not None, "start_worker() not called"
    _queue.put_nowait(job_id)


def startup_recover():
    """Mark jobs orphaned by a restart; re-enqueue still-queued jobs."""
    db = SessionLocal()
    try:
        for job in db.query(ExportJob).filter(ExportJob.status == "running").all():
            job.status = "interrupted"
            job.detail = "server restarted mid-export — press Retry to resume"
        db.commit()
        return [j.id for j in db.query(ExportJob).filter(ExportJob.status == "queued").all()]
    finally:
        db.close()


def start_worker():
    """Create the queue + singleton worker task; enqueue recovered jobs."""
    global _queue, _worker_task
    _queue = asyncio.Queue()
    for job_id in startup_recover():
        _queue.put_nowait(job_id)
    _worker_task = asyncio.create_task(_worker_loop())


async def _worker_loop():
    while True:
        job_id = await _queue.get()
        try:
            await _run_job(job_id)
        except Exception:
            logger.exception("Export job %d crashed", job_id)
            _update_job(job_id, status="failed", error="internal error (see server log)",
                        finished_at=datetime.now(timezone.utc))


async def _get_text(chapter_id: int) -> str:
    """Chapter text from cache, else scrape (3 attempts) and store."""
    db = SessionLocal()
    try:
        chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
        if chapter is None:
            raise RuntimeError(f"chapter {chapter_id} disappeared")
        if chapter.text:
            return chapter.text
        scraper = get_scraper_for_url(chapter.rr_url)
        if scraper is None:
            raise RuntimeError(f"no scraper for {chapter.rr_url}")
        last_err = None
        for attempt in range(3):
            try:
                text = await scraper.scrape_chapter_text(chapter.rr_url)
                chapter.text = text
                chapter.word_count = len(text.split())
                chapter.fetched_at = datetime.now(timezone.utc)
                db.commit()
                return text
            except Exception as e:
                last_err = e
                await asyncio.sleep(2 * (attempt + 1))
        raise RuntimeError(f"failed to fetch '{chapter.title}': {last_err}")
    finally:
        db.close()


async def _synthesize_chapter_wav(job_id, chapter_id, title, voice, speed, wav_path: Path):
    """Batch-synthesize one chapter to a PCM_16 WAV, yielding between batches."""
    text = await _get_text(chapter_id)
    text = f"{title}\n\n{text}"  # spoken chapter announcement, matches streaming
    segments: list[np.ndarray] = []
    for batch in textbatch.split_batches(text):
        await _wait_for_export_turn(job_id)
        segments.extend(await tts.synthesize_batch(batch, voice, speed))
    if not segments:
        raise RuntimeError(f"no audio produced for '{title}'")
    silence = np.zeros(int(tts.SAMPLE_RATE * 0.3), dtype=np.float32)
    parts = []
    for i, seg in enumerate(segments):
        parts.append(seg)
        if i < len(segments) - 1:
            parts.append(silence)
    sf.write(str(wav_path), np.concatenate(parts), tts.SAMPLE_RATE, subtype="PCM_16")


async def _download_cover(novel_id: int) -> tuple:
    """Best-effort cover download. Returns (bytes|None, ext)."""
    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        cover_url = novel.cover_url if novel else None
    finally:
        db.close()
    if not cover_url:
        return None, "jpg"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(cover_url)
            resp.raise_for_status()
            ext = "png" if "png" in resp.headers.get("content-type", "") else "jpg"
            return resp.content, ext
    except Exception as e:
        logger.warning("Cover download failed (continuing without): %s", e)
        return None, "jpg"


async def _run_job(job_id: int):
    _cancel_requested.discard(job_id)
    db = SessionLocal()
    try:
        job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
        if job is None or job.status == "canceled":
            return
        settings = db.query(Settings).first()
        audiobook_dir = settings.audiobook_dir if settings else ""
        plex_conf = (settings.plex_url, settings.plex_token, settings.plex_section_id) \
            if settings else ("", "", "")
        chapters = (db.query(Chapter)
                    .filter(Chapter.novel_id == job.novel_id,
                            Chapter.order >= job.start_order,
                            Chapter.order <= job.end_order)
                    .order_by(Chapter.order).all())
        plan = [(c.id, c.order, c.title) for c in chapters]
        params = (job.novel_id, job.novel_title, job.author,
                  job.start_order, job.end_order, job.voice, job.speed)
    finally:
        db.close()

    novel_id, novel_title, author, start_order, end_order, voice, speed = params
    if not plan:
        _update_job(job_id, status="failed", error="no chapters in range",
                    finished_at=datetime.now(timezone.utc))
        return
    if not audiobook_dir:
        _update_job(job_id, status="failed", error="audiobook folder not set in Settings",
                    finished_at=datetime.now(timezone.utc))
        return

    job_dir = EXPORT_DIR / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    _update_job(job_id, status="running", chapters_total=len(plan), error=None)

    try:
        done = 0
        for ch_id, order, title in plan:
            if job_id in _cancel_requested:
                raise ExportCanceled()
            wav_path = job_dir / f"chapter_{order:05d}.wav"
            if not wav_path.exists():  # retry-resume: skip finished chapters
                _update_job(job_id, detail=f"synthesizing ch. {done + 1}/{len(plan)}: {title}")
                await _synthesize_chapter_wav(job_id, ch_id, title, voice, speed, wav_path)
            done += 1
            _update_job(job_id, chapters_done=done)

        _update_job(job_id, detail="assembling M4B")
        basename = m4b.export_basename(novel_title, start_order, end_order)
        cover_bytes, cover_ext = await _download_cover(novel_id)
        chapter_wavs = [(title, job_dir / f"chapter_{order:05d}.wav")
                        for _, order, title in plan]
        m4b_path = await asyncio.to_thread(
            m4b.assemble_m4b, chapter_wavs, job_dir / f"{basename}.m4b",
            book_title=basename, author=author,
            cover_bytes=cover_bytes, cover_ext=cover_ext)

        Path(audiobook_dir).mkdir(parents=True, exist_ok=True)
        dest = Path(audiobook_dir) / m4b_path.name
        shutil.move(str(m4b_path), str(dest))

        plex_url, plex_token, plex_section = plex_conf
        if plex_url and plex_token and plex_section:
            try:
                await plex.trigger_refresh(plex_url, plex_token, plex_section)
                note = "done — Plex refresh triggered"
            except plex.PlexUnreachable:
                note = f"Audiobook saved, but {plex.PLEX_UNREACHABLE_MSG}"
            except Exception as e:
                note = f"Audiobook saved, but Plex refresh failed: {e}"
        else:
            note = "done — Plex not configured, skipped refresh"

        shutil.rmtree(job_dir, ignore_errors=True)
        _update_job(job_id, status="completed", detail=note, output_path=str(dest),
                    finished_at=datetime.now(timezone.utc))
    except ExportCanceled:
        _update_job(job_id, status="canceled", detail="canceled",
                    finished_at=datetime.now(timezone.utc))
    except Exception as e:
        logger.exception("Export job %d failed", job_id)
        _update_job(job_id, status="failed", error=str(e),
                    finished_at=datetime.now(timezone.utc))
```

Note: `_synthesize_chapter_wav` calls `tts.synthesize_batch(...)` via the module (`tts.` prefix) so tests can monkeypatch it.

Wire into `main.py` lifespan (inside `lifespan`, after `_retention_cleanup()`):
```python
    import export_worker
    export_worker.start_worker()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_export_worker.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the whole suite**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add export_worker.py main.py tests/test_export_worker.py
git commit -m "feat: export job runner - batch synthesis, resume, M4B assembly, Plex delivery"
```

---

### Task 10: API routes — exports router, settings fields, delete-cancels-jobs

**Files:**
- Create: `routers/exports.py`
- Modify: `routers/settings.py`, `routers/novels.py` (DELETE endpoint), `main.py` (mount router)
- Test: `tests/test_exports_api.py`

**Interfaces:**
- Consumes: `export_worker.enqueue/request_cancel`, `plex.list_libraries/PlexUnreachable`, `database.ExportJob`.
- Produces REST API:
  - `POST /api/novels/{novel_id}/export` body `{"start_order": int, "end_order": int, "voice": str, "speed": float}` → `{"job_id": int}`; 400 on bad range/speed/unset `audiobook_dir`; 409 on identical queued/running job.
  - `GET /api/exports` → `{"jobs": [ExportJobResponse...]}` (newest first, limit 20).
  - `POST /api/exports/{job_id}/cancel`, `POST /api/exports/{job_id}/retry` → updated job.
  - `GET /api/plex/libraries` → `{"libraries": [...]}`; 400 unconfigured; 503 with `PLEX_UNREACHABLE_MSG` when unreachable.
  - Settings GET/PUT include `audiobook_dir, plex_url, plex_token, plex_section_id`.

- [ ] **Step 1: Write the failing test**

`tests/test_exports_api.py`:
```python
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    import export_worker
    enqueued = []
    monkeypatch.setattr(export_worker, "enqueue", lambda job_id: enqueued.append(job_id))
    monkeypatch.setattr(export_worker, "start_worker", lambda: None)  # no bg task in tests
    from main import app
    with TestClient(app) as c:
        c.enqueued = enqueued
        yield c


@pytest.fixture()
def novel_with_chapters(client):
    import database
    db = database.SessionLocal()
    novel = database.Novel(title="API Novel", author="A",
                           rr_url="https://www.royalroad.com/fiction/666/api")
    db.add(novel); db.commit()
    for i in range(1, 6):
        db.add(database.Chapter(novel_id=novel.id, title=f"C{i}", order=i,
                                rr_url=f"https://www.royalroad.com/fiction/666/api/chapter/{i}/c"))
    db.commit()
    nid = novel.id
    yield nid
    db.query(database.ExportJob).delete()
    db.query(database.Chapter).filter_by(novel_id=nid).delete()
    db.query(database.Novel).filter_by(id=nid).delete()
    db.commit(); db.close()


def test_create_export_job(client, novel_with_chapters):
    resp = client.post(f"/api/novels/{novel_with_chapters}/export",
                       json={"start_order": 2, "end_order": 4,
                             "voice": "af_heart", "speed": 1.25})
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    assert client.enqueued == [job_id]

    jobs = client.get("/api/exports").json()["jobs"]
    assert jobs[0]["id"] == job_id
    assert jobs[0]["status"] == "queued"
    assert jobs[0]["chapters_total"] == 3


def test_duplicate_job_conflicts(client, novel_with_chapters):
    body = {"start_order": 1, "end_order": 5, "voice": "af_heart", "speed": 1.0}
    assert client.post(f"/api/novels/{novel_with_chapters}/export", json=body).status_code == 200
    assert client.post(f"/api/novels/{novel_with_chapters}/export", json=body).status_code == 409


def test_bad_range_rejected(client, novel_with_chapters):
    resp = client.post(f"/api/novels/{novel_with_chapters}/export",
                       json={"start_order": 4, "end_order": 2,
                             "voice": "af_heart", "speed": 1.0})
    assert resp.status_code == 400


def test_cancel_and_retry(client, novel_with_chapters):
    job_id = client.post(f"/api/novels/{novel_with_chapters}/export",
                         json={"start_order": 1, "end_order": 2,
                               "voice": "af_heart", "speed": 1.0}).json()["job_id"]
    assert client.post(f"/api/exports/{job_id}/cancel").json()["status"] == "canceled"
    assert client.post(f"/api/exports/{job_id}/retry").json()["status"] == "queued"


def test_settings_roundtrip_new_fields(client):
    resp = client.put("/api/settings", json={"plex_url": "http://localhost:32400",
                                             "plex_token": "tok"})
    assert resp.status_code == 200
    data = client.get("/api/settings").json()
    assert data["plex_url"] == "http://localhost:32400"
    assert data["audiobook_dir"].endswith("Audiobooks")


def test_plex_libraries_unconfigured(client):
    client.put("/api/settings", json={"plex_url": "", "plex_token": ""})
    assert client.get("/api/plex/libraries").status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_exports_api.py -v`
Expected: FAIL — 404s (`routers/exports.py` not mounted; settings fields missing).

- [ ] **Step 3: Implement**

Create `routers/exports.py`:
```python
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
```

In `routers/settings.py` add the four fields to both models and the update logic:
```python
# SettingsResponse — add:
    audiobook_dir: str
    plex_url: str
    plex_token: str
    plex_section_id: str

# UpdateSettingsRequest — add:
    audiobook_dir: str | None = None
    plex_url: str | None = None
    plex_token: str | None = None
    plex_section_id: str | None = None

# update_settings body — add before db.commit():
    if req.audiobook_dir is not None:
        settings.audiobook_dir = req.audiobook_dir.strip()
    if req.plex_url is not None:
        settings.plex_url = req.plex_url.strip().rstrip("/")
    if req.plex_token is not None:
        settings.plex_token = req.plex_token.strip()
    if req.plex_section_id is not None:
        settings.plex_section_id = req.plex_section_id.strip()
```

In `routers/novels.py`, find the `DELETE /api/novels/{id}` endpoint and add before the novel is deleted:
```python
    # Cancel any export jobs for this novel; the worker checks cancellation
    # at every batch boundary.
    from database import ExportJob
    import export_worker
    for job in db.query(ExportJob).filter(ExportJob.novel_id == novel_id,
                                          ExportJob.status.in_(("queued", "running"))).all():
        export_worker.request_cancel(job.id)
        if job.status == "queued":
            job.status = "canceled"
            job.detail = "novel deleted"
```

In `main.py`: change the routers import to `from routers import novels, chapters, progress, settings, exports` and add `app.include_router(exports.router)` with the other routers.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_exports_api.py -v`
Expected: PASS (6 tests). Note: `TestClient(app)` runs the lifespan — `start_worker` is monkeypatched to a no-op in the fixture, and DB init runs against the test DB.

- [ ] **Step 5: Run whole suite and commit**

Run: `.venv\Scripts\python.exe -m pytest -v` — all pass.
```bash
git add routers/exports.py routers/settings.py routers/novels.py main.py tests/test_exports_api.py
git commit -m "feat: export/cancel/retry/plex-libraries API, export settings fields"
```

---

### Task 11: Frontend — settings section (folder, Plex URL/token, library picker)

**Files:**
- Modify: `frontend/index.html` (settings modal), `frontend/app.js`, `frontend/style.css`

**Interfaces:**
- Consumes: `PUT /api/settings` (new fields), `GET /api/plex/libraries`.
- Produces: settings UI persists `audiobook_dir/plex_url/plex_token/plex_section_id`; "Load libraries" fills a `<select>`; errors surface via `showToast`.

No JS test infra exists — verification is manual (Step 3).

- [ ] **Step 1: Implement**

In `frontend/index.html`, inside the settings modal content (after the existing settings groups), add:
```html
<h3 class="settings-heading">Audiobook Export</h3>
<div class="setting-row">
  <label for="audiobook-dir">Audiobook folder</label>
  <input type="text" id="audiobook-dir" placeholder="E:\Plex\Audiobooks\Audiobooks"
         onchange="updateSetting('audiobook_dir', this.value)">
</div>
<div class="setting-row">
  <label for="plex-url">Plex server URL</label>
  <input type="text" id="plex-url" placeholder="http://localhost:32400"
         onchange="updateSetting('plex_url', this.value)">
</div>
<div class="setting-row">
  <label for="plex-token">Plex token</label>
  <input type="password" id="plex-token" placeholder="X-Plex-Token"
         onchange="updateSetting('plex_token', this.value)">
</div>
<div class="setting-row">
  <label for="plex-section">Plex library</label>
  <select id="plex-section" onchange="updateSetting('plex_section_id', this.value)">
    <option value="">— load libraries first —</option>
  </select>
  <button class="btn btn-small" onclick="loadPlexLibraries()">Load libraries</button>
</div>
```

In `frontend/app.js`:
- In `openSettings()`, after existing field population, add:
```javascript
    document.getElementById('audiobook-dir').value = state.settings.audiobook_dir || '';
    document.getElementById('plex-url').value = state.settings.plex_url || '';
    document.getElementById('plex-token').value = state.settings.plex_token || '';
    const sec = document.getElementById('plex-section');
    sec.innerHTML = state.settings.plex_section_id
        ? `<option value="${escapeHtml(state.settings.plex_section_id)}" selected>Library #${escapeHtml(state.settings.plex_section_id)} (saved)</option>`
        : '<option value="">— load libraries first —</option>';
```
- Add:
```javascript
async function loadPlexLibraries() {
    try {
        const data = await api('GET', '/api/plex/libraries');
        const sec = document.getElementById('plex-section');
        sec.innerHTML = '<option value="">— choose —</option>' + data.libraries.map(l =>
            `<option value="${escapeHtml(l.id)}" ${l.id === state.settings.plex_section_id ? 'selected' : ''}>${escapeHtml(l.title)} (${escapeHtml(l.type)})</option>`
        ).join('');
        showToast('Libraries loaded — pick your audiobook library');
    } catch (e) {
        showToast(e.message, 5000);
    }
}
```
(`api()` already surfaces the response `detail` — the 503 unreachable message reads "Plex is unreachable (is Docker running?)…".)

In `frontend/style.css`, follow the existing `.setting-row` styles; add if missing:
```css
.settings-heading { margin: 16px 0 8px; font-size: 0.95em; opacity: 0.8; }
.btn-small { padding: 4px 10px; font-size: 0.85em; }
```

- [ ] **Step 2: Start server and verify settings roundtrip**

Run: `.venv\Scripts\python.exe main.py` then open `http://localhost:8000`, open Settings.
Expected: folder field shows `E:\Plex\Audiobooks\Audiobooks`; entering Plex URL + token and clicking "Load libraries" lists your libraries (or shows the unreachable toast if Docker is down — verify that message too by stopping Plex); picking one persists after reload.

- [ ] **Step 3: Commit**

```bash
git add frontend/index.html frontend/app.js frontend/style.css
git commit -m "feat: audiobook export settings UI with Plex library picker"
```

---

### Task 12: Frontend — Save to Plex modal + exports badge/panel + README

**Files:**
- Modify: `frontend/index.html`, `frontend/app.js`, `frontend/style.css`, `README.md`

**Interfaces:**
- Consumes: `POST /api/novels/{id}/export`, `GET /api/exports`, cancel/retry endpoints, `state.currentNovel.effective_settings` (from `GET /api/novels`), `loadVoices()` data.
- Produces: novel-page "Save to Plex" button + modal (From/To/voice/speed); header badge + exports panel with progress/cancel/retry; 3s polling while a job is queued/running; completion/failure toast.

- [ ] **Step 1: Implement HTML**

In `frontend/index.html`:
- Novel detail header (next to the existing `⚙ Novel` button): `<button class="btn" id="save-plex-btn" onclick="openExportModal()">💾 Save to Plex</button>`
- Top bar (next to settings gear): `<button class="btn" id="exports-badge" style="display:none" onclick="openExportsPanel()">⏳ <span id="exports-badge-count"></span></button>`
- Two modals (same overlay markup pattern as the add-novel modal):
```html
<div class="modal-overlay" id="export-modal" style="display:none">
  <div class="modal">
    <h2>Save to Plex</h2>
    <div class="setting-row"><label>From chapter</label><input type="number" id="export-start" min="1"></div>
    <div class="setting-row"><label>To chapter</label><input type="number" id="export-end" min="1"></div>
    <div class="setting-row"><label>Voice</label><select id="export-voice"></select></div>
    <div class="setting-row"><label>Speed</label>
      <select id="export-speed">
        <option>0.5</option><option>0.75</option><option>1.0</option><option>1.25</option>
        <option>1.5</option><option>1.75</option><option>2.0</option>
      </select>
    </div>
    <p class="hint" id="export-name-preview"></p>
    <div class="modal-actions">
      <button class="btn" onclick="closeExportModal()">Cancel</button>
      <button class="btn btn-primary" onclick="startExport()">Export</button>
    </div>
  </div>
</div>
<div class="modal-overlay" id="exports-panel" style="display:none">
  <div class="modal">
    <h2>Exports</h2>
    <div id="exports-list"></div>
    <div class="modal-actions"><button class="btn" onclick="closeExportsPanel()">Close</button></div>
  </div>
</div>
```

- [ ] **Step 2: Implement JS**

Add to `frontend/app.js`:
```javascript
// ===== Save to Plex exports =====
let exportsPollTimer = null;
let lastJobStatuses = {};

function openExportModal() {
    const novel = state.currentNovel;
    if (!novel) return;
    if (!(state.settings.audiobook_dir || '').trim()) {
        showToast('Set your audiobook folder in Settings first', 5000);
        return;
    }
    document.getElementById('export-start').value = 1;
    document.getElementById('export-end').value = novel.total_chapters || 1;
    const eff = novel.effective_settings || {};
    const voiceSel = document.getElementById('export-voice');
    voiceSel.innerHTML = state.voices.map(v =>
        `<option value="${escapeHtml(v.id)}" ${v.id === eff.voice ? 'selected' : ''}>${escapeHtml(v.label)}</option>`).join('');
    document.getElementById('export-speed').value = String(eff.speed ?? 1.0);
    updateExportNamePreview();
    document.getElementById('export-start').oninput = updateExportNamePreview;
    document.getElementById('export-end').oninput = updateExportNamePreview;
    document.getElementById('export-modal').style.display = 'flex';
}

function updateExportNamePreview() {
    const s = document.getElementById('export-start').value || '?';
    const e = document.getElementById('export-end').value || '?';
    document.getElementById('export-name-preview').textContent =
        `${state.currentNovel.title} - Chapters ${s} - ${e}.m4b`;
}

function closeExportModal() { document.getElementById('export-modal').style.display = 'none'; }

async function startExport() {
    const novel = state.currentNovel;
    try {
        const resp = await api('POST', `/api/novels/${novel.id}/export`, {
            start_order: parseInt(document.getElementById('export-start').value, 10),
            end_order: parseInt(document.getElementById('export-end').value, 10),
            voice: document.getElementById('export-voice').value,
            speed: parseFloat(document.getElementById('export-speed').value),
        });
        closeExportModal();
        showToast('Export queued');
        startExportsPolling();
    } catch (e) {
        showToast('Export failed to start: ' + e.message, 5000);
    }
}

async function refreshExports() {
    let data;
    try { data = await api('GET', '/api/exports'); } catch { return; }
    const jobs = data.jobs || [];
    const active = jobs.filter(j => j.status === 'queued' || j.status === 'running');
    const badge = document.getElementById('exports-badge');
    badge.style.display = active.length ? '' : 'none';
    if (active.length) {
        const running = active.find(j => j.status === 'running');
        document.getElementById('exports-badge-count').textContent = running
            ? `${running.chapters_done}/${running.chapters_total}` : `${active.length} queued`;
    }
    for (const j of jobs) {
        const prev = lastJobStatuses[j.id];
        if (prev && prev !== j.status) {
            if (j.status === 'completed') showToast(`✅ Export done: ${j.novel_title}`, 6000);
            if (j.status === 'failed') showToast(`❌ Export failed: ${j.error || 'see Exports panel'}`, 8000);
        }
        lastJobStatuses[j.id] = j.status;
    }
    renderExportsList(jobs);
    if (!active.length) stopExportsPolling();
}

function renderExportsList(jobs) {
    const el = document.getElementById('exports-list');
    if (!el) return;
    el.innerHTML = jobs.length ? jobs.map(j => `
        <div class="export-row">
          <div>
            <strong>${escapeHtml(j.novel_title)}</strong> — Ch ${j.start_order}–${j.end_order}
            <span class="export-status export-${j.status}">${j.status}</span>
            <div class="hint">${j.status === 'running' ? `${j.chapters_done}/${j.chapters_total} · ` : ''}${escapeHtml(j.detail || j.error || '')}</div>
          </div>
          <div>
            ${(j.status === 'queued' || j.status === 'running') ? `<button class="btn btn-small" onclick="cancelExport(${j.id})">Cancel</button>` : ''}
            ${(j.status === 'failed' || j.status === 'interrupted' || j.status === 'canceled') ? `<button class="btn btn-small" onclick="retryExport(${j.id})">Retry</button>` : ''}
          </div>
        </div>`).join('') : '<p class="hint">No exports yet.</p>';
}

async function cancelExport(id) {
    try { await api('POST', `/api/exports/${id}/cancel`); refreshExports(); }
    catch (e) { showToast(e.message); }
}
async function retryExport(id) {
    try { await api('POST', `/api/exports/${id}/retry`); startExportsPolling(); }
    catch (e) { showToast(e.message); }
}

function startExportsPolling() {
    refreshExports();
    if (!exportsPollTimer) exportsPollTimer = setInterval(refreshExports, 3000);
}
function stopExportsPolling() {
    if (exportsPollTimer) { clearInterval(exportsPollTimer); exportsPollTimer = null; }
}
function openExportsPanel() { document.getElementById('exports-panel').style.display = 'flex'; refreshExports(); }
function closeExportsPanel() { document.getElementById('exports-panel').style.display = 'none'; }
```
Also: call `startExportsPolling()` once during app init (it stops itself when no jobs are active), and store voices in `state.voices` inside `loadVoices()` if not already kept there.

`frontend/style.css` additions:
```css
.export-row { display: flex; justify-content: space-between; align-items: center;
              gap: 8px; padding: 8px 0; border-bottom: 1px solid var(--border, #333); }
.export-status { margin-left: 6px; font-size: 0.8em; padding: 1px 8px; border-radius: 8px;
                 background: #444; }
.export-completed { background: #2c6e49; } .export-failed { background: #a4243b; }
.export-running { background: #1d6fa5; } .export-interrupted { background: #a66a00; }
.hint { font-size: 0.85em; opacity: 0.7; }
```

- [ ] **Step 3: Manual end-to-end verification (the real test)**

1. Start server, add/refresh a short novel (or reuse an existing one).
2. Set audiobook folder to a scratch dir (e.g. `D:\tmp\plextest`) in Settings.
3. Save to Plex → chapters 1–2 → Export. Watch badge progress; on completion verify `Title - Chapters 1 - 2.m4b` exists in the scratch dir, has 2 chapter markers (`ffprobe -show_chapters`), spoken chapter titles, and cover art.
4. Start another export (chapters 1–3 of a second novel), then press play on an uncached chapter of a different novel: playback must start within a few seconds; the exports panel shows "waiting for playback to idle".
5. Kill the server mid-export; restart; verify job shows `interrupted`; Retry resumes and completes without re-synthesizing finished chapters (watch log).
6. Point settings at your real folder + Plex, export, verify the book appears in Prologue after the auto-refresh. Stop Docker/Plex and export again: job completes with the "Plex is unreachable (is Docker running?)" detail.

- [ ] **Step 4: Update README**

Add a "Save to Plex (M4B export)" section to `README.md` under Features/Usage: what the button does, the fixed naming format, the settings fields (folder, Plex URL, token, library), priority behavior (exports never delay playback), retry/interrupted semantics, and the Docker-unreachable message.

- [ ] **Step 5: Commit**

```bash
git add frontend/ README.md
git commit -m "feat: Save to Plex UI - export modal, jobs badge/panel, README"
```

---

### Task 13: Ebook-to-Audiobook CLI `--plex` flag

**Repo:** `D:\Projects\Ebook-to-Audiobook` (all paths below relative to it)

**Files:**
- Create: `src/plex_delivery.py`, `tests/test_plex_delivery.py`, `tests/__init__.py` (empty)
- Modify: `src/cli.py`, `README.md`, `requirements.txt` (add `pytest` as a dev note or separate `requirements-dev.txt`)

**Interfaces:**
- Produces:
  - `plex_delivery.DEFAULT_PLEX_DIR = r"E:\Plex\Audiobooks\Audiobooks"`
  - `plex_delivery.sanitize_title(title: str) -> str` (same rules as web app)
  - `plex_delivery.plex_output_name(title: str, chapter_range: tuple | None) -> str` → `"Title.m4b"` or `"Title - Chapters X - Y.m4b"`
  - `plex_delivery.deliver(m4b_path: Path, plex_dir: str, title: str, chapter_range: tuple | None) -> Path` — moves + renames, creates dir.
  - `plex_delivery.trigger_refresh_from_env() -> str` — uses env `PLEX_URL`, `PLEX_TOKEN`, `PLEX_SECTION_ID` (stdlib `urllib`, 10s timeout); returns a human message; never raises.
- CLI: `--plex` (store_true), `--plex-dir` (default `DEFAULT_PLEX_DIR`).

- [ ] **Step 1: Write the failing test**

`tests/test_plex_delivery.py`:
```python
from pathlib import Path

from src.plex_delivery import sanitize_title, plex_output_name, deliver


def test_sanitize_title():
    assert sanitize_title('A/B: "C"?') == "A B C"


def test_output_name_without_range():
    assert plex_output_name("My Book") == "My Book.m4b"


def test_output_name_with_range():
    assert plex_output_name("My Book", (3, 7)) == "My Book - Chapters 3 - 7.m4b"


def test_deliver_moves_and_renames(tmp_path):
    src = tmp_path / "a.m4b"
    src.write_bytes(b"data")
    dest_dir = tmp_path / "lib"
    result = deliver(src, str(dest_dir), "The Count of Monte Cristo", None)
    assert result == dest_dir / "The Count of Monte Cristo.m4b"
    assert result.exists() and not src.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pip install pytest` then `.venv\Scripts\python.exe -m pytest tests/ -v`
Expected: FAIL — `No module named 'src.plex_delivery'`.

- [ ] **Step 3: Implement**

`src/plex_delivery.py`:
```python
"""Deliver a finished M4B into the Plex audiobook folder and trigger a rescan.

Plex runs in Docker, so refresh is section-level (container paths differ from
Windows paths). Refresh is best-effort: the file move is the real deliverable.
"""

import logging
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PLEX_DIR = r"E:\Plex\Audiobooks\Audiobooks"

PLEX_UNREACHABLE_MSG = (
    "Plex is unreachable (is Docker running?) — "
    "the audiobook will appear after the next library scan."
)

_INVALID = set('<>:"/\\|?*')


def sanitize_title(title: str) -> str:
    """Make a string safe as a Windows filename component."""
    cleaned = "".join(" " if (c in _INVALID or ord(c) < 32) else c for c in title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(" .")
    return cleaned or "Untitled"


def plex_output_name(title: str, chapter_range=None) -> str:
    base = sanitize_title(title)
    if chapter_range:
        base += f" - Chapters {chapter_range[0]} - {chapter_range[1]}"
    return base + ".m4b"


def deliver(m4b_path: Path, plex_dir: str, title: str, chapter_range=None) -> Path:
    """Move the M4B into the Plex folder under its library-facing name."""
    dest_dir = Path(plex_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / plex_output_name(title, chapter_range)
    shutil.move(str(m4b_path), str(dest))
    return dest


def trigger_refresh_from_env() -> str:
    """Trigger a Plex section refresh using PLEX_URL/PLEX_TOKEN/PLEX_SECTION_ID.

    Returns a human-readable status message; never raises.
    """
    url = os.environ.get("PLEX_URL", "").rstrip("/")
    token = os.environ.get("PLEX_TOKEN", "")
    section = os.environ.get("PLEX_SECTION_ID", "")
    if not (url and token and section):
        return "Plex refresh skipped (set PLEX_URL, PLEX_TOKEN, PLEX_SECTION_ID to enable)."
    refresh_url = (f"{url}/library/sections/{urllib.parse.quote(section)}/refresh"
                   f"?X-Plex-Token={urllib.parse.quote(token)}")
    try:
        with urllib.request.urlopen(refresh_url, timeout=10):
            pass
        return "Plex library refresh triggered."
    except (urllib.error.URLError, TimeoutError):
        return PLEX_UNREACHABLE_MSG
    except Exception as e:
        return f"Plex refresh failed ({e}) — file is saved; rescan manually."
```

In `src/cli.py`:
- Add to the Output & Display arg group (or a new "Plex" group):
```python
    out_group.add_argument(
        "--plex",
        action="store_true",
        help="After building, move the M4B into the Plex audiobook folder "
             "(named from the book title) and trigger a Plex rescan if "
             "PLEX_URL/PLEX_TOKEN/PLEX_SECTION_ID env vars are set.",
    )
    out_group.add_argument(
        "--plex-dir",
        default=None,
        help=f"Plex audiobook folder for --plex (default: {DEFAULT_PLEX_DIR}).",
    )
```
with `from src.plex_delivery import deliver, trigger_refresh_from_env, DEFAULT_PLEX_DIR` added to the imports.
- In `run()`, inside the success block after `result_path = build_m4b(...)` and before the summary print, add:
```python
        if opts.plex:
            chapter_range = None
            if opts.chapters:
                orders = [ch.index + 1 for ch in chapters_to_process]
                chapter_range = (min(orders), max(orders))
            result_path = deliver(
                result_path,
                opts.plex_dir or DEFAULT_PLEX_DIR,
                metadata.title,
                chapter_range,
            )
            print(f"  Moved to Plex folder: {result_path}")
            print(f"  {trigger_refresh_from_env()}")
```
(The summary print below already uses `result_path`, so it reports the final location.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/ -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Manual smoke test**

Run: `.venv\Scripts\python.exe main.py a.epub -o test_out.m4b --chapters 1 --plex --plex-dir D:\tmp\plextest`
Expected: after synthesis, output lands at `D:\tmp\plextest\The Hundred Reigns [Timeloop LitRPG] - Chapters 1 - 1.m4b` and the Plex message prints ("skipped (set PLEX_URL…)" if env unset). Delete the test file afterwards.

- [ ] **Step 6: Update README and commit**

Add a `--plex` section to `README.md` (flag behavior, env vars, Docker-unreachable message).
```bash
git add src/plex_delivery.py src/cli.py tests/ README.md
git commit -m "feat: --plex flag - deliver M4B to Plex folder and trigger rescan"
```

---

## Self-review notes (already applied)

- **Spec coverage:** settings/UI (T2, T11), text cache incl. playback population (T3), naming + sanitization (T4), M4B assembly from files (T5), 600-word batches (T6), Plex client + unreachable message (T7), priority gate incl. favorites sync and 90s heartbeat (T8), job engine with resume/interrupted/cancel/cover/Plex-note (T9), API incl. 409 duplicate + novel-delete cancel (T10), export UI + polling + toasts (T12), CLI flag (T13). Out-of-scope items from the spec are absent by design.
- **Type consistency:** `chapter_wavs: list[(title, Path)]`, `ExportJob` field names, `plex.list_libraries -> [{"id","title","type"}]`, and `split_batches` signatures are used identically across tasks.
- **Known simplifications:** exports API tests monkeypatch `export_worker.start_worker` to avoid a live background task in `TestClient`; frontend has no JS test rig, so Task 12's gate is the manual E2E checklist (including the restart-resume and Docker-down scenarios).
