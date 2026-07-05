# Novel TTS

Listen to web novels as audiobooks, read aloud by an AI voice, using your own
computer. Novel TTS runs a small website on your PC: you paste a link to a novel
from [Royal Road](https://www.royalroad.com) or [Ranobes](https://ranobes.net)
(or any site you add), and it turns the chapters into speech as you listen.
Because it runs on your machine, there are no subscriptions and no limits.

You can listen at your desk or from your phone, pick from a variety of voices,
and it always remembers where you left off. It can also package a range of
chapters into a real audiobook file and drop it straight into your Plex library.

## What you need

- A Windows PC with an NVIDIA graphics card (that's what makes the voice fast —
  it works without one, just much slower)
- Python 3.10, 3.11, or 3.12 (not 3.13)
- espeak-ng — a small speech helper the voice engine needs. Download the `.msi`
  installer from [espeak-ng releases](https://github.com/espeak-ng/espeak-ng/releases)
  and run it
- ffmpeg — needed for audiobook exports and smooth iPhone playback. Running
  `winget install ffmpeg` in a terminal works

## Setting it up

Open a terminal in this folder and run these once:

```bash
# 1. Make a private Python environment for the app
python -m venv .venv
.venv\Scripts\Activate.ps1

# 2. Install PyTorch with graphics-card support (for CUDA 12.1)
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121

# 3. Install everything else
pip install -r requirements.txt

# 4. Start it
python main.py
```

Then open `http://localhost:8000` in your browser. To start it in the future,
just run `python main.py` again (or use `RoyalRoadTTS.bat`).

## Everyday use

**Add a novel.** Click "+ Add Novel" and paste the novel's link. Not sure which
sites work? There's a "Check the supported sites" link right there.

**Listen.** Open a novel, tap a chapter, and it starts reading. The player at
the bottom has play/pause, skip, and jump buttons. Your spot is saved
automatically, and the Resume button takes you right back to it — from any
device.

**Favorites.** Tap the star on a novel to follow it. Favorites are checked for
new chapters automatically whenever you open the app, and the next few chapters
are prepared in advance so they start instantly. Novels you've started show how
many chapters you have left — a small count on the cover in grid view, or next
to the resume button in list view.

**Views.** The library can be a grid of covers or a compact list — the button
next to the sort menu switches between them. Hold and drag to arrange novels in
whatever order you like (with a mouse, just click and drag).

**Voices and speed.** The gear icon opens Settings. The Voices tab plays a
short sample of every voice so you can compare them and pick your favorite.
Speed can be adjusted any time, overall or per novel.

**From your phone.** On the same wi-fi, visit `http://YOUR-PC-IP:8000`. For
listening away from home, install [Tailscale](https://tailscale.com) on the PC
and your phone, then use the PC's Tailscale address instead.

## Saving audiobooks to Plex

Any novel can be exported as a proper audiobook file (M4B) with chapters and
cover art.

1. In Settings, open the Export tab. Set the folder where your audiobooks live.
   Optionally add your Plex server address and token, press "Load libraries",
   and pick your audiobook library — then Plex refreshes itself whenever a new
   book arrives.
2. On a novel's page, press "Save to Plex". Pick the first and last chapter,
   the voice, and the speed.
3. That's it. The export works quietly in the background — it never interrupts
   what you're listening to — and the file shows up in your audiobook folder
   named like `Book Title - Chapters 1 - 50.m4b`. Exporting the same range
   again replaces the old file.

A small progress badge appears at the top while an export runs; click it for
details. If an export stops partway (a failure, or the computer restarted),
open that badge and press Retry — it continues from where it stopped instead of
starting over. If Plex happens to be off when the export finishes, the file is
still saved and Plex will find it on its next library scan.

## Adding more sites

Support for each website comes from a small "scraper" file, and adding one is
very doable even if you don't code — an AI tool like Claude Code, Cursor, or
Codex can write it for you in a few minutes. The step-by-step guide is in
[ADDING-SITES.md](ADDING-SITES.md).

## If something goes wrong

- **"espeak-ng not found"** — install espeak-ng (see What you need) and restart
  the terminal.
- **Voice generation is very slow** — the app couldn't find your graphics card.
  Update your NVIDIA drivers and make sure you installed the CUDA version of
  PyTorch (step 2 above).
- **A site stopped working** — the website probably changed its layout. The fix
  is the same as adding a site: see [ADDING-SITES.md](ADDING-SITES.md).
- **Audio won't play on your phone** — in Settings, switch Playback Mode to
  "Full Chapter". It waits a little longer before starting but is the most
  reliable on mobile.

## More detail

Technical documentation — project layout, the scraper interface, the full API,
and how the export pipeline works — lives in
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## License

MIT
