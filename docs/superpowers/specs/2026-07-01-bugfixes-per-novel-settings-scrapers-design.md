# Bug Fixes, Per-Novel Settings, and Pluggable Scrapers — Design

**Date:** 2026-07-01
**Status:** Approved (user approved design in conversation)

## Context

Royal Road TTS is a self-hosted FastAPI + vanilla-JS web app that scrapes Royal
Road novels, synthesizes chapter audio with Kokoro TTS, and streams playback.
This spec covers seven user-reported issues plus a code-quality pass.

## 1. Bug: autoplay fails when next chapter is on the next page

**Root cause:** `playAdjacentChapter()` (frontend/app.js) fetches the full
chapter list when the target isn't in `state.chapters`, then calls
`playChapter(target.id)` — but `playChapter` starts with
`state.chapters.find(...)` and silently returns because the chapter isn't on
the currently loaded page.

**Fix:** Introduce a playback queue decoupled from the browsed list (see §8).
`playChapter` accepts a chapter object (not just an id) and never depends on
the currently rendered page. When autoplay crosses a page boundary while the
user is viewing that novel, advance `state.chapterPage` and re-render.

## 2. Bug: "Current" tag doesn't update until page refresh

**Root cause:** the badge renders only from the server's `is_current` flag at
chapter-list load time.

**Fix:** when playback of a chapter starts, update `is_current` flags in
client state and move the `current` class/badge in the DOM. Server persistence
is unchanged (progress is already saved separately).

## 3. Feature: Resume button

- **Novel detail view:** when `GET /api/progress/{novel_id}` returns saved
  progress, show a prominent "▶ Resume — Ch. N (mm:ss)" button under the
  header. Clicking plays that chapter and seeks to the saved position
  (position-restore logic already exists in `playFullFile`).
- **Library cards:** the existing "Ch. N" progress badge becomes a shortcut —
  clicking it opens the novel and resumes immediately.

## 4. Feature: per-novel settings

Per-novel overrides for **voice, speed, auto_play, chapter_sort**. Global
settings remain the defaults; a NULL override means "use global."

- **Schema:** four nullable columns on `novels`: `voice` (TEXT), `speed`
  (FLOAT), `auto_play` (BOOLEAN), `chapter_sort` (TEXT).
- **Migration:** `init_db()` gains a lightweight step that inspects existing
  columns (`PRAGMA table_info`) and issues `ALTER TABLE ... ADD COLUMN` for
  missing ones. No Alembic.
- **API:** `PATCH /api/novels/{novel_id}/settings` accepts any subset of the
  four fields; explicit `null` clears an override. Novel responses include the
  override values. Validation mirrors global settings (speed 0.5–2.0, sort
  asc/desc).
- **Resolution:** helper `effective_settings(novel, global_settings)` used by
  `list_chapters` (sort), `stream`/`synthesize` (voice), and returned to the
  frontend so playback code uses the playing novel's voice/speed/auto_play.
- **UI:** a ⚙ button on the novel detail view opens a per-novel settings
  modal: same controls as global settings but each select gets a
  "Default (…)" first option showing the inherited value; speed gets a
  "use default" reset. The global settings modal is unchanged.
- Playback (rate, autoplay decision, voice sent to synth) resolves from the
  novel being **played**, not the novel being browsed or the global object.

## 5. Feature: pluggable `scrapers/` directory

```
scrapers/
  __init__.py      # registry: discover_scrapers(), get_scraper_for_url(url)
  base.py          # BaseScraper ABC + shared HTTP/rate-limit helpers
  royalroad.py     # current scraper.py logic, moved
```

- **BaseScraper interface (async):**
  - `name: str` and `url_patterns: list[re.Pattern]` class attributes
  - `matches(url) -> bool` (default impl: any pattern matches)
  - `scrape_novel_metadata(url) -> dict` (title, author, cover_url,
    description, rr_url=canonical source URL)
  - `scrape_chapter_list(url) -> list[dict]` (title, rr_url, rr_chapter_id,
    order, published_at)
  - `scrape_chapter_text(url) -> str`
- **Shared helpers in base.py:** rate-limited GET (per-scraper lock,
  1 req/sec), default headers/user-agent — a new site scraper is ~100 lines.
- **Registry:** `pkgutil.iter_modules` over the package; modules that fail to
  import are logged and skipped (a broken user-added file must not take down
  the app). `get_scraper_for_url` returns the first scraper whose patterns
  match, else `None`.
- **Routers:** replace direct `scraper` imports with
  `get_scraper_for_url(url)`. Adding a novel with an unmatched URL returns 400
  listing supported sites. Chapter streaming/synthesis resolves the scraper
  from `novel.rr_url`.
- **DB:** `rr_url` columns keep their names (they store the source URL). No
  schema rename. `scraper.py` is deleted after the move.

## 6. Bug: double-tap zoom on mobile skip buttons

Add `touch-action: manipulation;` to `html, body` in style.css. Removes the
double-tap-to-zoom gesture (and its 300 ms tap delay) on all controls while
keeping pinch zoom and scrolling.

## 7. Bug: prefetch chain breaks on already-cached chapters

**Root cause:** `POST /api/chapters/{id}/synthesize` returns early with
`ready: true` when the file exists, skipping `_after_synthesis()` — so playing
a prefetched chapter never prefetches the following one, and autoplay hits a
cold cache every other chapter.

**Fix:** on the early-return path, still schedule `_after_synthesis()`
(prefetch next chapter if its file is missing; cleanup keeping
prev/current/next). Prefetch uses the effective per-novel voice.

## 8. Code-quality pass (bugs found during audit)

1. **Playback queue decoupled from browsing** — new `state.playback` holding
   the playing novel, its chapter list, and current chapter. Fixes: autoplay
   across pages (§1), and next/prev jumping into a *different* novel if the
   user browses novel B while listening to novel A. The mini-player and Media
   Session read from `state.playback`, not the browsed novel.
2. **HTML escaping** — `escapeHtml()` applied to all titles/authors/
   descriptions injected via `innerHTML` (library cards, chapter rows, player
   labels via `textContent` already safe).
3. **`seekRelative` clamp bug** — `Math.min(audio.duration || 0, ...)` resets
   playback to 0 when duration is unknown; only clamp to duration when it is
   a finite number.
4. **Dead code in `add_novel`** (routers/novels.py) — the first `existing`
   query is discarded and its filter is meaningless; remove it.
5. **`playFull` error path** — polling errors currently `break` and then play
   a file that may not exist; abort with a toast instead, and cap/backoff
   handled by existing 1.5 s interval.
6. **Module-level imports** — move `import asyncio` / `import soundfile`
   out of function bodies in routers/chapters.py.

Not in scope: N+1 queries in `list_novels` (fine for a personal library),
tray.py log handling, position-restore during instant-mode segment playback.

## Error handling

- Unmatched/invalid novel URL → 400 with supported-sites list.
- Broken scraper module → logged, skipped at discovery; app still serves
  other scrapers.
- Migration failures raise at startup (fail fast — DB file is local).

## Testing

Manual verification against the running app: autoplay across a page boundary,
Current badge movement, resume flows, per-novel sort/voice/speed/auto-play
overrides, prefetch chain (watch `temp_audio/`), double-tap on phone.
Plus `python -c` smoke imports and starting the server. No test suite exists
in this project; not introducing one in this pass.
