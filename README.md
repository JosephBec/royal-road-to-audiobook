# Novel TTS

A self-hosted web app that lets you listen to web novels — [Royal Road](https://www.royalroad.com), [Ranobes](https://ranobes.net), or any site you add a scraper for — with AI-generated narration powered by [Kokoro TTS](https://github.com/hexgrad/kokoro).

Add novels by URL, browse chapters, and listen with real-time audio synthesis on your GPU. Progress is tracked server-side — pick up where you left off from any device on your network.

---

## Features

- **Royal Road + Ranobes integration** — Add novels by URL, automatically scrapes metadata, cover art, and chapter lists
- **Pluggable scrapers** — Drop a new `.py` file in `scrapers/` to support additional sites (see below)
- **Per-novel settings** — Override voice, speed, auto-play, and chapter sort order for individual novels
- **Favorites** — Star the novels you follow: auto-refreshed and pre-downloaded on every visit; bookmark `/#favorites` to open straight into them
- **Library organization** — All/Favorites tabs, sort by recently listened/added/title, or long-press drag cards into a custom order
- **Resume** — One-click resume from the novel page or the library card badge
- **GPU-accelerated TTS** — Kokoro-82M with CUDA for fast synthesis (~30x realtime on RTX 2070)
- **Streaming playback** — Start listening within seconds (Mode A) or wait for full synthesis (Mode B)
- **Progress tracking** — Saves chapter + position automatically, persists across devices and restarts
- **Next-chapter prefetch** — Background synthesis of the next chapter for seamless transitions
- **Chapter refresh** — Re-crawl for new chapters at any time
- **15+ voices** — Male and female voices in American and British English
- **Adjustable speed** — 0.5x to 2.0x in 0.25 increments
- **Mobile-friendly** — Responsive design, accessible from phone via local network or Tailscale
- **Persistent mini player** — Always-visible transport controls with scrub bar
- **Dark theme** — Easy on the eyes for long listening sessions
- **Save to Plex (M4B export)** — Turn a chapter range into an M4B audiobook with chapter markers and cover art, dropped straight into your Plex library

## Requirements

- **Python 3.10–3.12** (Python 3.13+ not supported by Kokoro)
- **NVIDIA GPU** with CUDA support (CPU fallback available but slow)
- **espeak-ng** — Required by Kokoro for phoneme generation
- **ffmpeg** (optional) — Enables seamless Instant Play on iPhone via native
  HLS. Without it, Instant Play falls back to clip-by-clip playback with
  short gaps.

## Installation

### 1. Install System Dependencies

#### espeak-ng

- **Windows:** Download the `.msi` installer from [espeak-ng releases](https://github.com/espeak-ng/espeak-ng/releases) and run it.

### 2. Clone the Repository

```bash
git clone https://github.com/YourUser/royal-road-to-audiobook.git
cd royal-road-to-audiobook
```

### 3. Create a Virtual Environment

```bash
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# Linux / macOS
source .venv/bin/activate
```

### 4. Install PyTorch with CUDA

Visit [pytorch.org](https://pytorch.org/get-started/locally/) for the right command for your CUDA version.

For CUDA 12.1:
```bash
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
```

### 5. Install Dependencies

```bash
pip install -r requirements.txt
```

### 6. Start the Server

```bash
python main.py
```

The server starts on `http://0.0.0.0:8000`. Open `http://localhost:8000` in your browser.

## Usage

### Adding a Novel

1. Click **+ Add Novel** in the top bar
2. Paste a Royal Road fiction URL (e.g., `https://www.royalroad.com/fiction/64916/hell-difficulty-tutorial`)
3. The app scrapes the novel page, downloads the chapter list, and adds it to your library

### Listening

1. Click a novel card to open its chapter list
2. Click any chapter or its play button to start playback
3. The mini player at the bottom shows controls — play/pause, skip, scrub, and ±15/30 second jumps
4. Progress saves automatically every 10 seconds

### Refreshing for New Chapters

On the novel detail page, click **↻ Refresh** to re-crawl Royal Road for newly published chapters.

### Settings

Click the ⚙️ gear icon to configure:

- **Voice** — Choose from 15+ Kokoro voices
- **Speed** — 0.5x to 2.0x
- **Playback Mode:**
  - **Wait for File** (default) — Synthesizes the full chapter before playing. Short wait (~30-60s for long chapters), but reliable background playback on mobile.
  - **Instant Play** — Streams audio as it's synthesized. Starts playing within
    seconds. On Safari/iOS this uses a native HLS stream (seamless, works with
    the screen locked); other browsers fall back to sequential clip playback.
- **Auto-play** — Automatically advance to the next chapter when the current one ends
- **Chapter Sort Order** — Oldest-first or newest-first chapter lists

### Per-Novel Settings

On a novel's detail page, click **⚙ Novel** to override voice, speed, auto-play, or
chapter sort order for that novel only. Each control has a "Default" option that
falls back to the global setting; overrides apply immediately, including to the
chapter currently playing.

### Save to Plex (M4B export)

On a novel's detail page, click **💾 Save to Plex** to bundle a chapter range into
a single M4B audiobook file and hand it to Plex:

- Pick **From / To** chapter (defaults to the full novel), and a **Voice** and
  **Speed** (defaulting to that novel's effective settings). A live preview shows
  the exact output filename before you export.
- **Naming is fixed and not configurable:** `Title - Chapters X - Y.m4b` (no
  author, sanitized for Windows-illegal characters). Re-exporting the same range
  overwrites the existing file.
- The button (technically, the confirm action) requires an **audiobook folder**
  to be set in Settings first — you'll get a toast telling you to set it if it's
  empty.
- The file gets chapter markers (one per source chapter, titled and timed from
  the synthesized audio), embedded cover art, and `title`/`artist`/`genre` tags,
  then is moved into your audiobook folder and (if Plex is configured) triggers a
  library section refresh.

**Settings** (gear icon → "Audiobook Export"):

- **Audiobook folder** — where finished `.m4b` files are moved (e.g. your Plex
  library's audiobook folder)
- **Plex server URL** — e.g. `http://localhost:32400`
- **Plex token** — your `X-Plex-Token`
- **Plex library** — pick from a dropdown populated via "Load libraries" once
  URL + token are set

**Exports never delay playback.** Export jobs run on the same GPU worker as
interactive synthesis, but strictly at the lowest priority — a job yields
between chapter batches until playback isn't waiting on synthesis, every active
listener's next few chapters are already cached, and favorites sync isn't
running. The header shows a **⏳ Exports** badge with live progress
(`done/total` while running, or `N queued`) whenever a job is active; click it
to open the panel with per-job status, detail line, and Cancel/Retry.

- **Retry** re-queues a failed, interrupted, or canceled job and reuses any
  chapters it already finished synthesizing — it does not start over.
- **Interrupted** jobs are ones that were mid-export when the server restarted
  (e.g. you stopped it); Retry resumes them from where they left off.
- If Plex can't be reached when the export finishes (for example its Docker
  container isn't running), the job still completes and the file is still
  saved — the detail line reads *"Plex is unreachable (is Docker running?)"*
  and the library will pick it up on its next scan.

### Favorites

Star a novel (☆ on its card or detail page) to mark it as actively followed:

- On every site visit the server re-crawls favorites for new chapters and
  pre-downloads the next 3 chapters from your current position (at most once
  per 10 minutes; background work always yields to whatever you press play on).
- Favorites' pre-downloaded audio is kept until you listen past it.
  Non-favorites still pre-download 3 ahead while you're playing them, but that
  cache expires after 2 days — tuned for binge reads.
- Non-favorites are no longer auto-crawled when opened; use ↻ Refresh.
- Bookmark `http://<server>:8000/#favorites` to open the Favorites tab directly.

### Organizing the library

Tabs switch between All and ⭐ Favorites (favorites always group first in All).
The sort menu offers Recently Listened, Recently Added, Title A–Z, and Custom
Order — long-press and drag any card to build the custom order.

### Resume

If a novel has saved progress, its detail page shows a **▶ Resume** button with the
chapter and timestamp, and its library card badge (▶ Ch. N) becomes a one-click
resume shortcut.

### Accessing from Phone

The server binds to `0.0.0.0`, so it's accessible from any device on your local network:

```
http://<YOUR-PC-IP>:8000
```

For remote access outside your home network, install [Tailscale](https://tailscale.com) on both your PC and phone, then access via your Tailscale IP.

## Configuration

### Voice Configuration

Voices are defined in `config.yaml`. To add or remove voices, edit the file and restart the server:

```yaml
voices:
  - id: af_heart
    label: "Heart (Female, American)"
  - id: am_adam
    label: "Adam (Male, American)"

default_voice: af_heart
default_speed: 1.0
```

### Custom Port

```bash
python main.py --port 3000
```

### Bind to Localhost Only

```bash
python main.py --host 127.0.0.1
```

## Project Structure

```
royal-road-to-audiobook/
├── main.py              # FastAPI entry point + server startup
├── config.yaml          # Voice configuration
├── database.py          # SQLAlchemy models + SQLite
├── scrapers/
│   ├── __init__.py      # Scraper registry (auto-discovers site modules)
│   ├── base.py          # BaseScraper interface + shared HTTP helpers
│   ├── royalroad.py     # Royal Road scraper
│   └── ranobes.py       # Ranobes (ranobes.net) scraper
├── tts.py               # Kokoro TTS wrapper + streaming + temp files
├── routers/
│   ├── novels.py        # Novel CRUD + refresh
│   ├── chapters.py      # Chapter list + audio streaming + synthesis
│   ├── progress.py      # Playback progress tracking
│   ├── settings.py      # App settings (voice, speed, mode)
│   └── exports.py       # M4B export jobs + Plex library listing
├── export_worker.py     # Background export queue (lowest-priority GPU worker)
├── m4b.py                # M4B assembly: naming, chapter metadata, ffmpeg encode
├── plex.py               # Plex API client (library listing, section refresh)
├── frontend/
│   ├── index.html       # SPA shell
│   ├── app.js           # Frontend logic
│   └── style.css        # Dark theme styles
├── temp_audio/          # Synthesized audio cache (auto-managed)
├── requirements.txt
└── README.md
```

## Running on Startup (Windows)

### Option A: Task Scheduler

1. Open Task Scheduler → Create Basic Task
2. Name: "Royal Road TTS"
3. Trigger: "When the computer starts"
4. Action: Start a program
   - Program: `D:\Projects\royal-road-to-audiobook\.venv\Scripts\python.exe`
   - Arguments: `main.py`
   - Start in: `D:\Projects\royal-road-to-audiobook`

### Option B: Batch File

Create `start-server.bat`:
```batch
@echo off
cd /d D:\Projects\royal-road-to-audiobook
.venv\Scripts\python.exe main.py
```

Place a shortcut to this file in your Windows Startup folder (`shell:startup`).

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/novels` | List all novels |
| POST | `/api/novels` | Add novel by URL |
| DELETE | `/api/novels/{id}` | Remove novel |
| GET | `/api/novels/{id}/chapters` | Paginated chapter list |
| POST | `/api/novels/{id}/refresh` | Re-crawl for new chapters |
| GET | `/api/chapters/{id}/stream` | Stream/serve synthesized audio |
| GET | `/api/chapters/{id}/hls.m3u8` | Live HLS playlist (Instant Play, Safari/iOS) |
| GET | `/api/chapters/{id}/hls/{n}.aac` | HLS AAC segment |
| GET | `/api/chapters/{id}/status` | Check synthesis status |
| POST | `/api/chapters/{id}/synthesize` | Start background synthesis |
| GET | `/api/progress/{novel_id}` | Get reading progress |
| PUT | `/api/progress/{novel_id}` | Update reading progress |
| GET | `/api/settings` | Get app settings |
| PUT | `/api/settings` | Update app settings |
| PATCH | `/api/novels/{id}/settings` | Set/clear per-novel overrides + favorite flag |
| PUT | `/api/novels/order` | Save manual card order |
| POST | `/api/library/refresh-favorites` | Kick background favorites sync (10-min cooldown) |
| GET | `/api/voices` | List available voices |
| POST | `/api/novels/{id}/export` | Queue an M4B export job for a chapter range |
| GET | `/api/exports` | List active + recent export jobs with progress |
| POST | `/api/exports/{id}/cancel` | Cancel a queued or running export job |
| POST | `/api/exports/{id}/retry` | Re-queue a failed/interrupted/canceled job |
| GET | `/api/plex/libraries` | List Plex library sections (requires URL + token) |
| GET | `/` | Serve frontend |

## Adding a Scraper for Another Site

Scrapers live in `scrapers/` and are auto-discovered at startup — no registration
code needed. Create `scrapers/mysite.py` with a `BaseScraper` subclass:

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

Use `self._rate_limited_get(client, url)` for polite 1 req/sec fetching (see
`scrapers/royalroad.py` for a complete example). Restart the server and novels
from that site can be added by URL. A broken scraper file is logged and skipped;
it won't take the app down.

## Troubleshooting

### "espeak-ng not found"
Install espeak-ng and ensure it's on your PATH. On Windows, the MSI installer handles this.

### "CUDA not available"
- Update NVIDIA drivers
- Install the CUDA-enabled PyTorch build (not CPU-only)
- Check: `python -c "import torch; print(torch.cuda.is_available())"`

### Royal Road scraping fails
- Check your internet connection
- Royal Road may be temporarily down or rate-limiting
- The scraper rate-limits itself to 1 request/second

### Audio won't play on mobile
- Use **Wait for File** playback mode (Mode B) for reliable mobile playback
- Ensure your phone can reach the server IP

## License

MIT
