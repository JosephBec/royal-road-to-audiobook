# Royal Road TTS

A self-hosted web app that lets you listen to [Royal Road](https://www.royalroad.com) web novels with AI-generated narration powered by [Kokoro TTS](https://github.com/hexgrad/kokoro).

Add novels by URL, browse chapters, and listen with real-time audio synthesis on your GPU. Progress is tracked server-side — pick up where you left off from any device on your network.

---

## Features

- **Royal Road integration** — Add novels by URL, automatically scrapes metadata, cover art, and chapter lists
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

## Requirements

- **Python 3.10–3.12** (Python 3.13+ not supported by Kokoro)
- **NVIDIA GPU** with CUDA support (CPU fallback available but slow)
- **espeak-ng** — Required by Kokoro for phoneme generation

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
  - **Instant Play** — Streams audio as it's synthesized. Starts playing within seconds but may have issues with mobile background playback.
- **Auto-play** — Automatically advance to the next chapter when the current one ends

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
├── scraper.py           # Royal Road scraper
├── tts.py               # Kokoro TTS wrapper + streaming + temp files
├── routers/
│   ├── novels.py        # Novel CRUD + refresh
│   ├── chapters.py      # Chapter list + audio streaming + synthesis
│   ├── progress.py      # Playback progress tracking
│   └── settings.py      # App settings (voice, speed, mode)
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
| GET | `/api/chapters/{id}/status` | Check synthesis status |
| POST | `/api/chapters/{id}/synthesize` | Start background synthesis |
| GET | `/api/progress/{novel_id}` | Get reading progress |
| PUT | `/api/progress/{novel_id}` | Update reading progress |
| GET | `/api/settings` | Get app settings |
| PUT | `/api/settings` | Update app settings |
| GET | `/api/voices` | List available voices |
| GET | `/` | Serve frontend |

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
