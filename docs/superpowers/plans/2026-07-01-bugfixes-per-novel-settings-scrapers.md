# Bug Fixes, Per-Novel Settings & Pluggable Scrapers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix seven reported bugs/gaps (autoplay pagination, stale Current tag, missing resume, double-tap zoom, broken prefetch chain) and add per-novel settings plus a pluggable `scrapers/` directory.

**Architecture:** FastAPI backend (routers/, database.py SQLAlchemy+SQLite, tts.py Kokoro) serving a vanilla-JS SPA (frontend/). Scraping moves from a single `scraper.py` into a `scrapers/` package with a registry keyed by URL pattern. Per-novel settings are nullable override columns on `novels` resolved against the global `settings` row. Frontend playback gets a queue (`state.playback`) decoupled from the browsed chapter list.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, httpx, BeautifulSoup/lxml, vanilla JS, SQLite.

## Global Constraints

- No test framework exists and none is introduced; every task verifies via `python -c` smoke imports, starting the server, or curl. Manual UI verification at the end.
- Speed valid range 0.5–2.0; chapter_sort values `asc`/`desc`; NULL override = inherit global.
- `rr_url` columns keep their names (they store the source URL for any site).
- Windows host; venv python at `.venv\Scripts\python.exe`. Server: `python main.py` on port 8000.
- Spec: `docs/superpowers/specs/2026-07-01-bugfixes-per-novel-settings-scrapers-design.md`.

---

### Task 0: Initialize git baseline

**Files:** none (repo metadata only). `.gitignore` already exists.

- [ ] **Step 1:** `git init` in project root, verify `.gitignore` covers `.venv/`, `data.db`, `temp_audio/`, `server.log`, `__pycache__/` (add any missing lines).
- [ ] **Step 2:** `git add -A && git commit -m "chore: baseline before bugfix/feature pass"`.

### Task 1: Per-novel override columns + migration + effective_settings

**Files:**
- Modify: `database.py`

**Interfaces:**
- Produces: `Novel.voice/speed/auto_play/chapter_sort` (all nullable), `effective_settings(novel, settings) -> dict` with keys `voice, speed, auto_play, chapter_sort`.

- [ ] **Step 1:** Add nullable columns to `Novel`:

```python
    # Per-novel setting overrides; NULL = use global Settings default
    voice = Column(String, nullable=True)
    speed = Column(Float, nullable=True)
    auto_play = Column(Boolean, nullable=True)
    chapter_sort = Column(String, nullable=True)
```

- [ ] **Step 2:** Add migration + helper, call migration from `init_db()` after `create_all`:

```python
from sqlalchemy import text, inspect as sa_inspect

def _migrate_schema():
    """Add columns introduced after initial release (SQLite has no Alembic here)."""
    inspector = sa_inspect(engine)
    existing = {c["name"] for c in inspector.get_columns("novels")}
    new_columns = {"voice": "TEXT", "speed": "FLOAT", "auto_play": "BOOLEAN", "chapter_sort": "TEXT"}
    with engine.begin() as conn:
        for name, ddl_type in new_columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE novels ADD COLUMN {name} {ddl_type}"))

def effective_settings(novel: "Novel", settings: "Settings") -> dict:
    """Resolve per-novel overrides against global settings (None = inherit)."""
    return {
        "voice": novel.voice if novel.voice is not None else (settings.voice if settings else "af_heart"),
        "speed": novel.speed if novel.speed is not None else (settings.speed if settings else 1.0),
        "auto_play": novel.auto_play if novel.auto_play is not None else (settings.auto_play if settings else True),
        "chapter_sort": novel.chapter_sort if novel.chapter_sort is not None else (settings.chapter_sort if settings else "asc"),
    }
```

- [ ] **Step 3:** Verify: `.venv\Scripts\python.exe -c "from database import init_db, effective_settings; init_db(); import sqlite3; print([r[1] for r in sqlite3.connect('data.db').execute('PRAGMA table_info(novels)')])"` — output includes the four new columns.
- [ ] **Step 4:** Commit `feat: per-novel setting override columns + effective_settings`.

### Task 2: `scrapers/` package with registry; delete `scraper.py`

**Files:**
- Create: `scrapers/__init__.py`, `scrapers/base.py`, `scrapers/royalroad.py`
- Modify: `routers/novels.py`, `routers/chapters.py` (imports only in this task)
- Delete: `scraper.py`

**Interfaces:**
- Produces: `scrapers.get_scraper_for_url(url) -> BaseScraper | None`, `scrapers.supported_sites() -> list[str]`, `BaseScraper` async methods `scrape_novel_metadata(url)`, `scrape_chapter_list(url)`, `scrape_chapter_text(url)` returning the same dict shapes as the old module functions.

- [ ] **Step 1:** `scrapers/base.py` — ABC + shared rate-limited HTTP:

```python
"""Base scraper interface and shared HTTP helpers.

To add a site: create scrapers/<site>.py with a BaseScraper subclass; it is
auto-discovered. See royalroad.py for a template.
"""
import asyncio
import re
from abc import ABC, abstractmethod

import httpx

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": USER_AGENT}


class BaseScraper(ABC):
    """One instance per site. Subclasses set `name` and `url_patterns`."""

    name: str = "base"
    url_patterns: list[re.Pattern] = []

    def __init__(self):
        self._last_request_time = 0.0
        self._rate_lock = asyncio.Lock()

    def matches(self, url: str) -> bool:
        return any(p.search(url) for p in self.url_patterns)

    async def _rate_limited_get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        """GET with 1 req/sec rate limit per scraper."""
        async with self._rate_lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_request_time
            if elapsed < 1.0:
                await asyncio.sleep(1.0 - elapsed)
            self._last_request_time = asyncio.get_event_loop().time()
        response = await client.get(url, headers=HEADERS, follow_redirects=True)
        response.raise_for_status()
        return response

    @abstractmethod
    async def scrape_novel_metadata(self, url: str) -> dict:
        """Return {title, author, cover_url, description, rr_url(canonical)}."""

    @abstractmethod
    async def scrape_chapter_list(self, novel_url: str) -> list[dict]:
        """Return [{title, rr_url, rr_chapter_id, order, published_at}]."""

    @abstractmethod
    async def scrape_chapter_text(self, chapter_url: str) -> str:
        """Return chapter plain text, paragraphs separated by blank lines."""
```

- [ ] **Step 2:** `scrapers/royalroad.py` — move all logic from `scraper.py` into `class RoyalRoadScraper(BaseScraper)` with `name = "Royal Road"`, `url_patterns = [re.compile(r"https?://(?:www\.)?royalroad\.com/fiction/\d+")]`; module-level functions become methods (`_parse_rr_url` stays a module helper). Body identical to current `scraper.py` except `_rate_limited_get` comes from the base class.
- [ ] **Step 3:** `scrapers/__init__.py` — discovery + registry:

```python
"""Scraper registry. Drop a new <site>.py with a BaseScraper subclass here."""
import importlib
import logging
import pkgutil

from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)
_scrapers: list[BaseScraper] | None = None


def discover_scrapers() -> list[BaseScraper]:
    global _scrapers
    if _scrapers is not None:
        return _scrapers
    _scrapers = []
    pkg = importlib.import_module("scrapers")
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        if mod_info.name == "base":
            continue
        try:
            module = importlib.import_module(f"scrapers.{mod_info.name}")
        except Exception:
            logger.exception("Skipping broken scraper module: %s", mod_info.name)
            continue
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, BaseScraper) and obj is not BaseScraper:
                _scrapers.append(obj())
                logger.info("Registered scraper: %s", obj.name)
    return _scrapers


def get_scraper_for_url(url: str) -> BaseScraper | None:
    return next((s for s in discover_scrapers() if s.matches(url)), None)


def supported_sites() -> list[str]:
    return [s.name for s in discover_scrapers()]
```

- [ ] **Step 4:** Update routers to use the registry (full rewiring of call sites lands in Tasks 3–4; here swap imports and add a small helper used by both):
  - `routers/novels.py`: replace `from scraper import ...` with `from scrapers import get_scraper_for_url, supported_sites`; in `add_novel` resolve `scraper = get_scraper_for_url(req.url)`; if `None` → `HTTPException(400, f"No scraper supports this URL. Supported sites: {', '.join(supported_sites())}")`; call `scraper.scrape_novel_metadata(...)` / `scraper.scrape_chapter_list(...)`. Same resolution in `refresh_novel` from `novel.rr_url`.
  - `routers/chapters.py`: replace `from scraper import scrape_chapter_text` with `from scrapers import get_scraper_for_url`; where text is scraped, resolve from `chapter.rr_url` and 400 if no scraper matches.
- [ ] **Step 5:** Delete `scraper.py`. Verify: `.venv\Scripts\python.exe -c "from scrapers import discover_scrapers, get_scraper_for_url; print([s.name for s in discover_scrapers()]); print(get_scraper_for_url('https://www.royalroad.com/fiction/12345/x'))"` — prints `['Royal Road']` and a RoyalRoadScraper instance. Also `python -c "import main"`.
- [ ] **Step 6:** Commit `feat: pluggable scrapers package with URL-based registry`.

### Task 3: Per-novel settings API + effective settings in responses

**Files:**
- Modify: `routers/novels.py`, `routers/chapters.py`

**Interfaces:**
- Consumes: `effective_settings` from Task 1.
- Produces: `PATCH /api/novels/{id}/settings` → `{"settings": {...overrides}, "effective_settings": {...}}`; `NovelResponse` gains `settings: dict` and `effective_settings: dict`.

- [ ] **Step 1:** In `routers/novels.py` add:

```python
class NovelSettingsRequest(BaseModel):
    voice: str | None = None
    speed: float | None = None
    auto_play: bool | None = None
    chapter_sort: str | None = None


def _novel_settings_payload(novel: Novel, db: Session) -> dict:
    settings = db.query(Settings).first()
    return {
        "settings": {"voice": novel.voice, "speed": novel.speed,
                     "auto_play": novel.auto_play, "chapter_sort": novel.chapter_sort},
        "effective_settings": effective_settings(novel, settings),
    }


@router.patch("/{novel_id}/settings")
async def update_novel_settings(novel_id: int, req: NovelSettingsRequest, db: Session = Depends(get_db)):
    """Set or clear per-novel overrides. Explicit null clears (inherits global)."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    provided = req.model_fields_set
    if "speed" in provided and req.speed is not None and not (0.5 <= req.speed <= 2.0):
        raise HTTPException(status_code=400, detail="Speed must be between 0.5 and 2.0")
    if "chapter_sort" in provided and req.chapter_sort not in (None, "asc", "desc"):
        raise HTTPException(status_code=400, detail="Chapter sort must be 'asc' or 'desc'")
    for field in ("voice", "speed", "auto_play", "chapter_sort"):
        if field in provided:
            setattr(novel, field, getattr(req, field))
    db.commit()
    return _novel_settings_payload(novel, db)
```

(Import `Settings` and `effective_settings` from `database`.)
- [ ] **Step 2:** Extend `NovelResponse` with `settings: dict | None = None` and `effective_settings: dict | None = None`; populate in `list_novels` and `add_novel` via `_novel_settings_payload`.
- [ ] **Step 3:** In `routers/chapters.py`: `list_chapters` resolves sort via `effective_settings(novel, settings)["chapter_sort"]`; `stream_chapter` and `start_synthesis` resolve `voice` the same way (query the novel; they already query the chapter).
- [ ] **Step 4:** Verify with server running: `curl -s -X PATCH localhost:8000/api/novels/1/settings -H "Content-Type: application/json" -d "{\"chapter_sort\":\"desc\"}"` returns overrides + effective; `curl -s localhost:8000/api/novels` shows both dicts; PATCH with `{"chapter_sort":null}` clears it.
- [ ] **Step 5:** Commit `feat: per-novel settings API and effective-settings resolution`.

### Task 4: Prefetch chain fix + backend cleanups

**Files:**
- Modify: `routers/chapters.py`, `routers/novels.py`

**Interfaces:** none new; behavior change on `POST /api/chapters/{id}/synthesize` early-return path.

- [ ] **Step 1:** `routers/chapters.py`: move `import asyncio` and `import soundfile as sf` to module top. In `start_synthesis`, hoist the neighbor-gathering block (`next_chapters`, `prev_ch`, `keep_ids`, `prefetch_id/url`) and `_after_synthesis()` definition ABOVE the `if status["ready"]` early return; guard prefetch with `not temp_path_for_chapter(prefetch_id).exists()`; on the ready path do `asyncio.create_task(_after_synthesis())` before returning `{**status, "mode": "full"}`.
- [ ] **Step 2:** `_after_synthesis` scrapes via `get_scraper_for_url(prefetch_url)`; skip prefetch (log warning) if no scraper.
- [ ] **Step 3:** `routers/novels.py`: delete the dead first `existing = db.query(Novel).filter(Novel.rr_url.contains(...))...` block in `add_novel` (the post-scrape canonical-URL check stays).
- [ ] **Step 4:** Verify: restart server; synthesize a short chapter twice — second call returns `ready: true` AND `temp_audio/` gains the next chapter's file shortly after (prefetch fired). `git commit -m "fix: prefetch next chapter even when current is cached; backend cleanups"`.

### Task 5: Frontend playback queue refactor (fixes autoplay-across-pages, wrong-novel nav, stale Current tag, seek clamp, playFull error path)

**Files:**
- Modify: `frontend/app.js`

**Interfaces:**
- Produces: `state.playback = { novel, chapters, chapter, settings }`; `playChapter(chapter, novel)` (object args); `markCurrentChapter(chapterId)`; `loadPlaybackQueue(novelId)`.

- [ ] **Step 1:** Add to state: `playback: { novel: null, chapters: [], chapter: null, settings: null }`. Remove `currentChapterId`/`currentNovelId` fields and replace all reads: `state.currentChapterId` → `state.playback.chapter?.id`, `state.currentNovelId` → `state.playback.novel?.id`. Guard comparisons like `if (state.currentChapterId !== chapterId)` become `if (state.playback.chapter?.id !== chapterId)`.
- [ ] **Step 2:** New helper:

```javascript
async function loadPlaybackQueue(novelId) {
    const data = await api('GET', `/api/novels/${novelId}/chapters?page=1&per_page=10000`);
    // Queue is always ascending regardless of display sort
    return data.chapters.slice().sort((a, b) => a.order - b.order);
}
```

- [ ] **Step 3:** `playChapter(chapter, novel = state.currentNovel)`: first lines become

```javascript
async function playChapter(chapter, novel = state.currentNovel) {
    if (!chapter || !novel) return;
    if (state.playback.novel?.id !== novel.id) {
        try { state.playback.chapters = await loadPlaybackQueue(novel.id); }
        catch (e) { showToast('Failed to load chapter list: ' + e.message); return; }
        state.playback.novel = novel;
    }
    state.playback.chapter = chapter;
    state.playback.settings = novel.effective_settings || null;
    markCurrentChapter(chapter.id);
    ...
```

Callers in `renderChapters` pass the chapter object: `playChapter(state.chapters.find(c => c.id === id))`. Player title reads `state.playback.novel.title` / `state.playback.chapter.title`.
- [ ] **Step 4:** `playAdjacentChapter(direction)` uses the queue only:

```javascript
async function playAdjacentChapter(direction) {
    const { chapter, chapters, novel } = state.playback;
    if (!chapter || !novel) return;
    const target = chapters.find(c => c.order === chapter.order + direction);
    if (!target) { showToast(direction > 0 ? 'No next chapter' : 'No previous chapter'); return; }
    await playChapter(target, novel);
    followPlaybackPage(target);
}
```

`followPlaybackPage(target)`: if `state.currentNovel?.id === novel.id` and target id isn't in `state.chapters`, compute the page from the effective sort (`asc: Math.ceil(order/50)`, `desc: Math.ceil((state.chapterTotal - order + 1)/50)` — store `state.chapterTotal = data.total` in `loadChapters`), set `state.chapterPage`, `await loadChapters()`, then `markCurrentChapter(target.id)`.
- [ ] **Step 5:** `markCurrentChapter(chapterId)` — client-side badge/class move:

```javascript
function markCurrentChapter(chapterId) {
    if (state.currentNovel?.id !== state.playback.novel?.id) return;
    state.chapters.forEach(c => { c.is_current = c.id === chapterId; });
    document.querySelectorAll('#chapter-list .chapter-row').forEach(row => {
        const isCur = parseInt(row.dataset.id) === chapterId;
        row.classList.toggle('current', isCur);
        const badge = row.querySelector('.current-badge');
        if (isCur && !badge) {
            const b = document.createElement('span');
            b.className = 'current-badge';
            b.textContent = 'Current';
            row.insertBefore(b, row.querySelector('.chapter-play-btn'));
        } else if (!isCur && badge) badge.remove();
    });
}
```

- [ ] **Step 6:** Per-novel playback settings: `applyPlaybackRate()` uses `state.playback.settings?.speed ?? state.settings.speed`; the `ended` handler checks `state.playback.settings?.auto_play ?? state.settings.auto_play`; `updateMediaSession` reads title/novel from `state.playback`.
- [ ] **Step 7:** Fix `seekRelative`:

```javascript
function seekRelative(seconds) {
    if (!state.audio.src) return;
    const max = isFinite(state.audio.duration) ? state.audio.duration : Infinity;
    state.audio.currentTime = Math.max(0, Math.min(max, state.audio.currentTime + seconds));
}
```

- [ ] **Step 8:** Fix `playFull` error path: replace `catch (e) { break; }` with `catch (e) { showToast('Synthesis check failed: ' + e.message); loadingEl.style.display = 'none'; state.isSynthesizing = false; return; }`.
- [ ] **Step 9:** Verify in browser: play last chapter of page 1 → autoplay continues onto page 2 and list follows; Current badge moves without refresh; +30 before metadata loads doesn't reset to 0; browse another novel while listening — Next stays in the playing novel. Commit `fix: playback queue decoupled from browsing; current badge, seek, autoplay fixes`.

### Task 6: Resume button (novel view + library badge)

**Files:**
- Modify: `frontend/index.html`, `frontend/app.js`, `frontend/style.css`

**Interfaces:**
- Consumes: `playChapter(chapter, novel)`, `loadPlaybackQueue` from Task 5; `GET /api/progress/{novel_id}`.

- [ ] **Step 1:** index.html — insert between `.novel-header` and `#novel-description`: `<button id="btn-resume" class="primary-btn resume-btn" style="display:none;"></button>`. style.css: `.resume-btn { display: block; margin: 0 auto 16px; }` (visibility still controlled inline).
- [ ] **Step 2:** app.js:

```javascript
async function updateResumeButton() {
    const btn = document.getElementById('btn-resume');
    btn.style.display = 'none';
    if (!state.currentNovel) return;
    try {
        const progress = await api('GET', `/api/progress/${state.currentNovel.id}`);
        if (!progress.chapter_id) return;
        const pos = progress.position_seconds > 5 ? ` (${formatTime(progress.position_seconds)})` : '';
        btn.textContent = `▶ Resume — Ch. ${progress.chapter_order}${pos}`;
        btn.style.display = '';
        btn.onclick = () => resumeNovel(state.currentNovel, progress.chapter_id);
    } catch (e) { /* no progress — leave hidden */ }
}

async function resumeNovel(novel, chapterId) {
    try {
        const queue = await loadPlaybackQueue(novel.id);
        const target = queue.find(c => c.id === chapterId);
        if (!target) { showToast('Saved chapter not found'); return; }
        state.playback.novel = novel;
        state.playback.chapters = queue;
        await playChapter(target, novel);
    } catch (e) { showToast('Resume failed: ' + e.message); }
}
```

Call `updateResumeButton()` at the end of `openNovel` (position-seek already happens in `playFullFile` via saved progress).
- [ ] **Step 3:** Library badge shortcut: in `renderLibrary`, give the badge `data-novel-id` and a click handler with `e.stopPropagation()` that calls `openNovel(id, { resume: true })`; `openNovel` gains `opts` param and, when `opts.resume`, after `updateResumeButton()` fetches progress and calls `resumeNovel`.
- [ ] **Step 4:** Verify: resume button appears with correct chapter/time; clicking resumes at saved position; badge on library card resumes directly. Commit `feat: resume buttons on novel view and library cards`.

### Task 7: Per-novel settings UI

**Files:**
- Modify: `frontend/index.html`, `frontend/app.js`

**Interfaces:**
- Consumes: `PATCH /api/novels/{id}/settings` (Task 3), `novel.settings` / `novel.effective_settings` from novel responses.

- [ ] **Step 1:** index.html — add `<button id="btn-novel-settings" class="secondary-btn">⚙ Novel</button>` next to `#btn-refresh`, and a new modal:

```html
<div id="modal-novel-settings" class="modal" style="display:none;">
    <div class="modal-content">
        <h3>Novel Settings</h3>
        <p id="ns-novel-name"></p>
        <div class="setting-row"><label for="ns-voice">Voice</label><select id="ns-voice"></select></div>
        <div class="setting-row"><label>Speed</label>
            <div class="speed-control">
                <button id="ns-speed-down" class="small-btn">−</button>
                <span id="ns-speed-value">Default</span>
                <button id="ns-speed-up" class="small-btn">+</button>
                <button id="ns-speed-reset" class="small-btn" title="Use default">↺</button>
            </div>
        </div>
        <div class="setting-row"><label for="ns-autoplay">Auto-play Next</label>
            <select id="ns-autoplay"><option value="">Default</option><option value="true">On</option><option value="false">Off</option></select>
        </div>
        <div class="setting-row"><label for="ns-sort">Chapter Sort</label>
            <select id="ns-sort"><option value="">Default</option><option value="asc">Oldest first</option><option value="desc">Newest first</option></select>
        </div>
        <div class="modal-actions"><button id="btn-ns-close" class="primary-btn">Done</button></div>
    </div>
</div>
```

- [ ] **Step 2:** app.js — open/populate/update:

```javascript
function openNovelSettings() {
    const novel = state.currentNovel;
    if (!novel) return;
    const ov = novel.settings || {};
    document.getElementById('ns-novel-name').textContent = novel.title;
    const globalVoiceLabel = state.voices.find(v => v.id === state.settings.voice)?.label || state.settings.voice;
    document.getElementById('ns-voice').innerHTML =
        `<option value="">Default (${globalVoiceLabel})</option>` +
        state.voices.map(v => `<option value="${v.id}" ${v.id === ov.voice ? 'selected' : ''}>${v.label}</option>`).join('');
    document.getElementById('ns-speed-value').textContent =
        ov.speed != null ? `${ov.speed.toFixed(2)}x` : `Default (${state.settings.speed.toFixed(2)}x)`;
    document.getElementById('ns-autoplay').value = ov.auto_play == null ? '' : String(ov.auto_play);
    document.getElementById('ns-sort').value = ov.chapter_sort ?? '';
    document.getElementById('modal-novel-settings').style.display = 'flex';
}

async function updateNovelSetting(field, value) {
    const novel = state.currentNovel;
    if (!novel) return;
    try {
        const result = await api('PATCH', `/api/novels/${novel.id}/settings`, { [field]: value });
        novel.settings = result.settings;
        novel.effective_settings = result.effective_settings;
        if (state.playback.novel?.id === novel.id) {
            state.playback.settings = result.effective_settings;
            applyPlaybackRate();
        }
        if (field === 'chapter_sort') { state.chapterPage = 1; await loadChapters(); }
        openNovelSettings(); // re-render modal values
    } catch (e) { showToast('Failed to save: ' + e.message); }
}
```

Listeners in `setupEventListeners`: `btn-novel-settings` → `openNovelSettings`; `btn-ns-close` + backdrop click → hide; `ns-voice` change → `updateNovelSetting('voice', e.target.value || null)`; `ns-autoplay` change → `updateNovelSetting('auto_play', e.target.value === '' ? null : e.target.value === 'true')`; `ns-sort` change → `updateNovelSetting('chapter_sort', e.target.value || null)`; `ns-speed-down/up` → step from `(novel.settings.speed ?? state.settings.speed)` by ±0.05 clamped 0.5–2.0; `ns-speed-reset` → `updateNovelSetting('speed', null)`.
- [ ] **Step 3:** Verify: set one novel to newest-first + different voice; other novels unaffected; "Default" options show inherited values; reset returns to global. Commit `feat: per-novel settings UI`.

### Task 8: HTML escaping + double-tap zoom fix

**Files:**
- Modify: `frontend/app.js`, `frontend/style.css`

- [ ] **Step 1:** app.js top helpers:

```javascript
function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
```

Apply `escapeHtml()` to every interpolated title/author/label in `renderLibrary` (title, author, alt attr) and `renderChapters` (chapter title). Voice labels in the two settings modals too (they come from config.yaml).
- [ ] **Step 2:** style.css — after the base reset block:

```css
html, body { touch-action: manipulation; }
button, input, select { touch-action: manipulation; }
```

- [ ] **Step 3:** Verify: titles containing `<b>` or `&` render literally; on phone, rapid taps on -15/+30 no longer zoom (pinch still works). Commit `fix: escape user-visible HTML; disable double-tap zoom on controls`.

### Task 9: Final verification

- [ ] **Step 1:** `.venv\Scripts\python.exe -c "import main"`; start server; exercise: add novel (bad URL → 400 with supported sites), chapter list sort per novel, play → prefetch chain (`temp_audio/` gains next file even when playing cached chapters), resume, PATCH settings.
- [ ] **Step 2:** Update `README.md`: scrapers directory ("drop a .py in scrapers/"), per-novel settings, resume.
- [ ] **Step 3:** Final commit `docs: update README for scrapers + per-novel settings`.

## Self-review notes

- Spec coverage: §1→T5, §2→T5, §3→T6, §4→T1+T3+T7, §5→T2, §6→T8, §7→T4, §8 quality items→T2/T4/T5/T8. ✔
- Type consistency: `effective_settings` dict keys used identically in T1/T3/T5/T7; `playChapter(chapter, novel)` object signature consistent across T5/T6. ✔
