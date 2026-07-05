# Development Reference

Technical details for working on Novel TTS. For everyday use, see the
[README](../README.md). For adding a site the easy way, see
[ADDING-SITES.md](../ADDING-SITES.md).

## Project structure

```
royal-road-to-audiobook/
├── main.py              # FastAPI entry point, misc endpoints, server startup
├── config.yaml          # Voice list and defaults
├── database.py          # SQLAlchemy models + SQLite (novels, chapters, progress,
│                        #   settings, export_jobs)
├── library_sync.py      # Background favorites sync (re-crawl + prefetch)
├── tts.py               # Kokoro TTS wrapper: streaming, temp files, batch seam
├── textbatch.py         # Splits text into ~600-word synthesis batches
├── export_worker.py     # Save-to-Plex job engine (queue, priority gate, resume)
├── m4b.py               # M4B assembly: naming, ffmetadata chapters, ffmpeg encode
├── plex.py              # Plex client: list libraries, section refresh
├── tray.py              # Windows system-tray launcher
├── scrapers/
│   ├── __init__.py      # Registry (auto-discovers site modules)
│   ├── base.py          # BaseScraper interface + rate-limited HTTP helper
│   ├── royalroad.py     # Royal Road
│   └── ranobes.py       # Ranobes
├── routers/             # API routes: novels, chapters, progress, settings, exports
├── frontend/            # Vanilla JS single-page app (index.html, app.js, style.css)
├── tests/               # pytest suite (isolated SQLite via NOVEL_TTS_DB env var)
└── docs/                # This file, PRD, design specs and plans
```

Runtime folders (created as needed, git-ignored): `temp_audio/` (streaming cache),
`export_jobs/` (per-job WAVs while an export runs), `voice_demos/` (cached voice
samples), `data.db`, `server.log`.

## Scraper interface

Scrapers live in `scrapers/` and are auto-discovered at startup. Create
`scrapers/mysite.py` with a `BaseScraper` subclass:

```python
import re
from scrapers.base import BaseScraper

class MySiteScraper(BaseScraper):
    name = "My Site"
    url_patterns = [re.compile(r"https?://(?:www\.)?mysite\.com/novel/\d+")]

    async def scrape_novel_metadata(self, url: str) -> dict:
        # return {"title", "author", "cover_url", "description", "rr_url"(canonical URL)}
        ...

    async def scrape_chapter_list(self, novel_url: str) -> list[dict]:
        # return [{"title", "rr_url"(chapter URL), "rr_chapter_id", "order", "published_at"}]
        ...

    async def scrape_chapter_text(self, chapter_url: str) -> str:
        # return plain text, paragraphs separated by blank lines
        ...
```

Use `self._rate_limited_get(client, url)` for polite 1 request/second fetching
(see `scrapers/royalroad.py` for a complete example). Strip navigation, ads, and
author notes from chapter text — only story text should reach the TTS. A broken
scraper file is logged and skipped; it won't take the app down.

## API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/novels` | List all novels |
| POST | `/api/novels` | Add novel by URL |
| DELETE | `/api/novels/{id}` | Remove novel (cancels its export jobs) |
| GET | `/api/novels/{id}/chapters` | Paginated chapter list |
| POST | `/api/novels/{id}/refresh` | Re-crawl for new chapters |
| PATCH | `/api/novels/{id}/settings` | Per-novel overrides + favorite flag |
| PUT | `/api/novels/order` | Save manual card order |
| GET | `/api/chapters/{id}/stream` | Serve synthesized audio |
| GET | `/api/chapters/{id}/hls.m3u8` | Live HLS playlist (Instant Play, Safari/iOS) |
| GET | `/api/chapters/{id}/hls/{n}.aac` | HLS AAC segment |
| GET | `/api/chapters/{id}/status` | Synthesis status |
| POST | `/api/chapters/{id}/synthesize` | Start background synthesis |
| GET | `/api/chapters/{id}/segments` | Streaming segment state |
| GET/PUT | `/api/progress/{novel_id}` | Playback progress |
| GET/PUT | `/api/settings` | App settings (incl. audiobook dir, Plex fields) |
| GET | `/api/voices` | Voice list from config.yaml |
| GET | `/api/voices/{id}/demo` | Cached voice demo clip (generated on first request) |
| POST | `/api/novels/{id}/export` | Queue a Save-to-Plex M4B export |
| GET | `/api/exports` | Recent/active export jobs |
| POST | `/api/exports/{id}/cancel` | Cancel a queued/running export |
| POST | `/api/exports/{id}/retry` | Retry a failed/interrupted/canceled export |
| GET | `/api/plex/libraries` | Plex library list (for settings picker) |
| GET | `/api/scrapers` | Installed scrapers (name + URL patterns) |
| POST | `/api/library/refresh-favorites` | Kick background favorites sync |
| GET | `/api/library/sync-status` | Whether the favorites sync is running |
| GET | `/` | Frontend |

## Export pipeline notes

Exports run at strictly lowest priority on the single shared TTS worker:
between every synthesis batch the worker waits until playback is idle, active
listeners' next-3 prefetch is cached, and the favorites sync is done. Job
artifacts live in `export_jobs/<id>/`; retry resumes by skipping finished
chapter WAVs; on server restart, running jobs are marked interrupted and can be
retried. Plex refresh is section-level (Docker-safe) and non-fatal. Design
history lives in `docs/superpowers/specs/` and `docs/superpowers/plans/`.

## Tests

```bash
.venv\Scripts\python.exe -m pytest -q
```

`tests/conftest.py` points the app at a throwaway SQLite database via the
`NOVEL_TTS_DB` environment variable before any project module is imported.
The suite fakes TTS and network where needed; the M4B assembly tests run real
ffmpeg and verify chapters with ffprobe.

## Voices

Voices are defined in `config.yaml`. Add or remove entries and restart the
server. Demo clips are synthesized once per voice into `voice_demos/` the first
time someone plays them from Settings → Voices; delete the folder to force
regeneration.
