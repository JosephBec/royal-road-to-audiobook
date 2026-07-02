# Product Requirements Document
## Royal Road TTS Web App + EPUB CLI Narrator

**Version:** 1.0  
**Date:** 2026-03-10  
**Status:** Draft

---

## Overview

Two separate tools built around the Kokoro TTS engine:

1. **EPUB CLI** — A standalone terminal script that narrates EPUB files locally, outputting audio files chapter by chapter.
2. **Royal Road Web App** — A locally-hosted web application accessible from any device on the network (or via Tailscale), that tracks web novels from Royal Road, synthesizes chapter audio on demand with streaming playback, and remembers reading progress server-side.

These are completely independent codebases with no shared modules.

---

## Part 1: EPUB CLI Tool

### Summary

A terminal script that takes an EPUB file as input and synthesizes each chapter to audio using Kokoro TTS. Simple, no web server, no persistence.

### Functional Requirements

- Accept an EPUB file path as a CLI argument
- Parse the EPUB into ordered chapters using `ebooklib` + `beautifulsoup4`
- Strip HTML tags, clean whitespace, and prepare clean text per chapter
- Synthesize each chapter using Kokoro TTS, outputting one audio file per chapter
- Show a `tqdm` progress bar per chapter and overall
- Support CLI flags for:
  - `--voice` — Kokoro voice name (default: configurable at top of script)
  - `--speed` — playback speed multiplier (default: 1.0)
  - `--output-dir` — where to save audio files (default: `./output/`)
  - `--format` — audio format, e.g. `wav` or `mp3` (default: `wav`)
- Skip chapters that are clearly front matter (cover, TOC, copyright) using heuristics (very short text, known title patterns)
- Name output files as `01_chapter-title.wav`, `02_...`, etc.
- Print a summary when done (total chapters, total estimated duration, output location)

### Non-Goals

- No web UI
- No streaming
- No progress persistence
- No Royal Road integration

### Tech Stack

- Python 3.10+
- `kokoro`, `ebooklib`, `beautifulsoup4`, `lxml`, `soundfile`, `numpy`, `tqdm`
- Single file: `epub_narrator.py`

---

## Part 2: Royal Road TTS Web App

### Summary

A self-hosted web app that lets the user add Royal Road novels by URL, browse chapters, and listen to synthesized audio with real-time streaming. Progress is tracked server-side. Accessible from phone via local network or Tailscale.

---

### Architecture

```
[Browser / Phone]
      |
      | HTTP / WebSocket
      v
[Python Backend — FastAPI]
      |
      |-- SQLite DB (novels, chapters, progress)
      |-- Kokoro TTS engine (runs in-process or subprocess)
      |-- Royal Road scraper (httpx + BeautifulSoup)
      v
[Audio streamed as chunks over HTTP]
```

- **Backend:** Python + FastAPI
- **Frontend:** Single-page app (vanilla JS or lightweight framework) served by FastAPI
- **Database:** SQLite via SQLAlchemy (single file, zero config)
- **TTS:** Kokoro running locally, generating audio in chunks
- **Scraping:** `httpx` + `BeautifulSoup4` for Royal Road
- **Networking:** Runs on `0.0.0.0` so it's reachable on local network; optionally exposed via Tailscale

---

### Data Model

#### Novel
| Field | Type | Notes |
|---|---|---|
| id | int PK | |
| title | text | Pulled from Royal Road |
| author | text | |
| rr_url | text unique | Royal Road novel URL |
| cover_url | text | Cover image URL from RR |
| description | text | Novel blurb |
| total_chapters | int | Known count |
| last_refreshed | datetime | Last crawl time |
| created_at | datetime | |

#### Chapter
| Field | Type | Notes |
|---|---|---|
| id | int PK | |
| novel_id | int FK | |
| rr_chapter_id | text | Royal Road's chapter ID |
| title | text | |
| order | int | Position in novel |
| rr_url | text | Direct chapter URL |
| word_count | int | Approximate |
| published_at | datetime | From Royal Road |
| fetched_at | datetime | When text was scraped |

#### Progress
| Field | Type | Notes |
|---|---|---|
| id | int PK | |
| novel_id | int FK | |
| chapter_id | int FK | Last chapter listened to |
| position_seconds | float | Playback position within chapter |
| updated_at | datetime | |

---

### Features

#### Library View

- Card grid layout, one card per novel
- Each card shows:
  - Cover art (fetched from Royal Road)
  - Title and author
  - Progress indicator (e.g. "Chapter 47 / 312")
  - Last listened timestamp
- Top bar with:
  - "Add Novel" button (opens a URL paste modal)
  - App title

#### Adding a Novel

- User pastes a Royal Road novel URL (e.g. `https://www.royalroad.com/fiction/12345/novel-name`)
- Backend scrapes the novel page to get: title, author, cover image URL, description
- Scrapes the chapter list (paginated if needed) and stores all chapters
- Returns the new novel card to the UI immediately
- Shows a loading indicator while scraping

#### Novel Detail View

- Auto-refreshes chapter list on open (background crawl, shows spinner if new chapters found)
- Manual "Refresh" button to re-crawl at any time
  - Shows spinner during crawl
  - Shows toast: "3 new chapters found" or "Already up to date"
- Chapter list, 50 chapters per page with pagination controls
- Each chapter row shows:
  - Chapter number and title
  - Word count (approximate)
  - Published date
  - A play button
  - "Last listened" badge if it's the current progress chapter
- Clicking a chapter or its play button starts playback

#### Audio Playback and Streaming

- When a chapter is selected, the backend begins TTS synthesis in the background
- Audio is streamed to the browser as it is generated, chunk by chunk
- Playback begins as soon as the first chunk is ready (no waiting for full synthesis)
- While synthesis is in progress, the duration shown is "--:--" or a loading indicator
- Once synthesis of the full chapter completes, the player updates to show the real total duration
- Audio format: WAV or OGG Opus (browser-compatible, no re-encoding needed if possible)
- The backend does NOT cache synthesized audio — every play request re-synthesizes

#### Mini Player (Persistent Bottom Bar)

Always visible at the bottom of the screen while a chapter is loaded. Contains:

- **Novel title** and **chapter title** (truncated if needed)
- **Scrub bar** — shows playback position; updates to show real duration once synthesis completes
- **Transport controls:**
  - Previous chapter
  - Back 15 seconds
  - Play / Pause
  - Forward 30 seconds
  - Next chapter
- **Auto-play toggle** — when enabled, next chapter begins automatically when current chapter ends
- If next/previous chapter audio hasn't been synthesized yet, the player shows a loading indicator and begins playback when ready

#### Settings Panel

Accessible from a gear icon. Contains:

- **Voice selector** — dropdown of configured voices (voices defined in a `config.yaml` or `config.json` file; admin can add/remove voices from that file without touching code)
- **Speed control** — slider or stepper (e.g. 0.5x to 2.0x in 0.25 increments)
- Settings are stored server-side in the DB (single user, no login needed) and persist across sessions

#### Progress Tracking

- Progress (chapter + position in seconds) is saved to the server automatically:
  - Every 10 seconds during playback
  - On pause
  - On chapter change
- When returning to a novel, playback resumes from the saved position
- Progress survives browser cache clears, device switches, and app restarts

---

### API Endpoints (Backend)

| Method | Path | Description |
|---|---|---|
| GET | `/api/novels` | List all novels |
| POST | `/api/novels` | Add novel by URL |
| DELETE | `/api/novels/{id}` | Remove novel |
| GET | `/api/novels/{id}/chapters` | Paginated chapter list |
| POST | `/api/novels/{id}/refresh` | Re-crawl for new chapters |
| GET | `/api/chapters/{id}/stream` | Stream synthesized audio (chunked HTTP) |
| GET | `/api/progress/{novel_id}` | Get reading progress for novel |
| PUT | `/api/progress/{novel_id}` | Update reading progress |
| GET | `/api/settings` | Get app settings (voice, speed) |
| PUT | `/api/settings` | Update app settings |
| GET | `/` | Serve frontend SPA |

---

### Voice Configuration

Voices are defined in `config.yaml` at the project root:

```yaml
voices:
  - id: af_heart
    label: "Heart (Female, American)"
  - id: am_adam
    label: "Adam (Male, American)"
  - id: bf_emma
    label: "Emma (Female, British)"

default_voice: af_heart
default_speed: 1.0
```

The backend reads this file at startup. To add or remove voices, edit the file and restart the server.

---

### Networking / Access from Phone

- Server binds to `0.0.0.0:8000` so it is reachable on the local network at `http://<PC-IP>:8000`
- For remote access: user installs Tailscale on both PC and phone, then accesses `http://<tailscale-ip>:8000`
- No authentication required (single user, trusted network)
- Optional: a `--port` CLI flag for the server

---

### Tech Stack

| Layer | Technology |
|---|---|
| Backend framework | FastAPI (Python) |
| Database | SQLite + SQLAlchemy |
| TTS | Kokoro |
| Scraping | httpx + BeautifulSoup4 |
| Audio streaming | FastAPI `StreamingResponse` with chunked transfer |
| Frontend | Vanilla JS + HTML/CSS (served by FastAPI as static files) |
| Networking | Local LAN + optional Tailscale |

---

### Project Structure

```
royalroad-tts/
├── main.py                  # FastAPI app entry point
├── config.yaml              # Voice config
├── database.py              # SQLAlchemy models and DB init
├── scraper.py               # Royal Road scraper
├── tts.py                   # Kokoro TTS wrapper + streaming logic
├── routers/
│   ├── novels.py
│   ├── chapters.py
│   ├── progress.py
│   └── settings.py
├── frontend/
│   ├── index.html
│   ├── app.js
│   └── style.css
├── requirements.txt
└── README.md
```

---

### Out of Scope (v1)

- User accounts / authentication
- Audio caching / download for offline
- Sleep timer
- Search Royal Road from within the app
- Support for sites other than Royal Road
- Mobile app (native iOS/Android)
- Dark/light mode toggle (can pick one default)

---

### Playback Architecture

The user has a CUDA GPU with ~30x realtime synthesis speed (a 30-min chapter takes ~1 min to generate). The app supports two playback modes toggled in Settings:

#### Mode A: "Instant Play" (Stream via MSE)
- Backend synthesizes audio chunk by chunk and streams via chunked HTTP
- Frontend uses MediaSource Extensions (MSE) to begin playback within seconds of the first chunk arriving
- Simultaneously, the backend writes chunks to a temp `.wav` file on disk
- Once synthesis completes, the frontend **seamlessly swaps** the MSE source for a standard `<audio src="...">` pointing to the completed file, preserving playback position
- After swap, background/screen-off playback works reliably on iPhone Safari
- Risk: a very brief hiccup at swap time (acceptable tradeoff)

#### Mode B: "Wait for File" (Reliable, Short Wait)
- Backend synthesizes the full chapter to a temp file before serving
- Frontend polls `/api/chapters/{id}/status` until synthesis is complete, then loads the file URL into a standard `<audio>` element
- With the user's GPU, typical wait is under 60 seconds for a long chapter
- Screen-off playback works immediately and reliably from the start
- No swap complexity

#### Settings Toggle
- A "Playback Mode" toggle in the Settings panel switches between Mode A and Mode B
- Default: Mode B (more reliable, wait is short with GPU)
- Setting is persisted server-side

#### Next-Chapter Prefetch
- In both modes, once a chapter begins playing the backend immediately begins synthesizing the next chapter in the background and saves it to a temp file
- When the user advances to the next chapter (manually or via auto-play), it loads instantly from the pre-generated file
- Prefetch is always active regardless of playback mode

#### Temp File Management
- Synthesized temp files are stored in a `./temp_audio/` directory
- Files are named by chapter ID (e.g. `chapter_42.wav`)
- At most 3 files are kept at a time: previous chapter, current chapter, next chapter
- Older files are deleted automatically when no longer needed

---

### Implementation Notes

- **iPhone Safari background audio:** Requires a standard HTML5 `<audio>` element (not Web Audio API alone). Mode B guarantees this. Mode A achieves it after the swap completes.
- **Duration display:** Unknown during synthesis. Frontend polls `/api/chapters/{id}/status` which returns `{ ready: bool, duration_seconds: float | null }`. Once `ready` is true, the scrub bar updates with real duration.
- **RR scraping:** Royal Road is public, no login required. Rate limit to 1 req/sec to be polite.
- **espeak-ng:** Must be installed system-wide (Kokoro dependency). README must include Windows install instructions.
- **Windows service:** README will include instructions to run the server on startup using either Task Scheduler or a simple `.bat` file.
- **Server binding:** `0.0.0.0:8000` for LAN access. Tailscale for remote access from phone outside home network.
