# Save to Plex — M4B Export Design

**Date:** 2026-07-04
**Repos affected:** `royal-road-to-audiobook` (primary), `Ebook-to-Audiobook` (CLI `--plex` flag only)

## Goal

One-button export of a chapter range to an M4B audiobook that lands in the user's
Plex audiobook folder (`E:\Plex\Audiobooks\Audiobooks`, consumed via Prologue) and
triggers a Plex library rescan — without ever degrading interactive streaming,
which always has priority.

## Decisions (settled with user)

- **Plex rescan:** via Plex API. Plex runs in Docker, so container paths differ from
  Windows paths → trigger a **whole-section refresh** (no `path=` param).
- **Chapter selection:** contiguous range only (From X / To Y).
- **File naming:** `Title - Chapters X - Y.m4b`. Not configurable. No author in the name.
- **Execution:** shared single TTS worker with **batch-level yielding** (no parallel
  export process in v1; job engine must not preclude adding one later).
- **No reuse of the streaming audio cache** (voice/speed may differ). A job MAY reuse
  its *own* per-chapter WAVs on retry (exact-parameter artifacts).
- **Export bakes speed into the audio** (streaming synthesizes at 1.0 and adjusts
  client-side; export is the first place speed permanently alters a file).
- Chapter numbers in names/markers = `Chapter.order` (position in the novel's list).
- Spoken chapter-title announcement at the start of each chapter (matches streaming).

## A. Settings

New `Settings` columns (via `_migrate_schema`) + an "Audiobook Export" section in the
settings UI:

| column | type | default |
|---|---|---|
| `audiobook_dir` | TEXT | `E:\Plex\Audiobooks\Audiobooks` |
| `plex_url` | TEXT | `""` (e.g. `http://localhost:32400`) |
| `plex_token` | TEXT | `""` |
| `plex_section_id` | TEXT | `""` |

UI: when URL + token are set, a dropdown loads libraries via server proxy
(`GET /api/plex/libraries` → Plex `/library/sections`), user picks the audiobook
section. A "Test" action reports connectivity errors inline.

## B. Data model

- `chapters.text` TEXT NULL — scraped chapter text cache. Populated on first scrape
  (export or playback), reused thereafter. Politeness: exports of N chapters scrape
  each chapter at most once, ever, at the existing 1 req/s rate limit.
- New `export_jobs` table: `id, novel_id, novel_title, author, start_order, end_order,
  voice, speed, status, chapters_done, chapters_total, detail, output_path, error,
  created_at, finished_at`.
  - `status`: `queued | running | completed | failed | interrupted | canceled`.
  - `detail`: transient human string ("synthesizing ch. 12/50", "waiting for playback
    to idle", "assembling M4B", "plex refresh failed (non-fatal)").
- Job artifacts live in `export_jobs/<job_id>/` in the repo root — **not**
  `temp_audio/` (which is wiped on startup): `chapter_<order>.wav` (PCM_16 mono 24kHz)
  + `manifest.json` (params snapshot). Deleted on success; kept on failure for retry.
- On startup, any job left `running` → `interrupted`.

## C. Export pipeline (background worker, one job at a time, FIFO)

Per chapter in range:
1. Text: from `chapters.text`, else scrape via the novel's scraper and store.
   Scrape failures retry 3× then fail the job.
2. Skip if `chapter_<order>.wav` already exists in the job dir (retry/resume).
3. Synthesize in **batches of ~600 words** (split on paragraph boundaries). Before
   each batch: `await _wait_for_export_turn()` (section D). Each batch is one
   `run_in_executor` call to the existing pipeline (`split_pattern=r'\n+'`, chosen
   voice, chosen speed). 300ms silence joins segments, matching streaming output.
4. Write chapter WAV, free arrays.

Assembly (after all chapters):
5. ffmpeg concat (demuxer, `-c copy`) → single WAV in job dir → encode AAC 64k mono
   into `.m4b` with FFMETADATA chapter markers (per-chapter titles + start/end from
   WAV durations) and embedded cover art (downloaded from `novel.cover_url`; skip on
   failure). Logic ported from Ebook-to-Audiobook `audiobook_builder.py`, adapted to
   read chapter audio from files instead of RAM. New module: `m4b.py`.
6. Tags: `title` and `album` = `"{sanitized_title} - Chapters {X} - {Y}"`,
   `artist` = author, `genre` = Audiobook.
7. Move `.m4b` to `audiobook_dir`. Filename = same sanitized base + `.m4b`.
   Sanitization: strip/replace Windows-illegal chars `<>:"/\|?*`, trim dots/spaces.
   If target filename exists, overwrite (same range re-export = intentional).
8. Plex: `GET {plex_url}/library/sections/{plex_section_id}/refresh?X-Plex-Token=…`
   (10s timeout). Failure is **non-fatal**: job completes with warning in `detail`.
   Connection refused / timeout (e.g. the Docker engine hosting Plex is down) gets a
   distinct message: "Audiobook saved, but Plex is unreachable (is Docker running?) —
   it will appear after the next library scan." Same distinction in the CLI flag and
   in the settings "Test" action.
9. Delete job dir; mark `completed`.

## D. Priority: `_wait_for_export_turn()`

Export proceeds only when ALL hold (else `asyncio.sleep(2)` and re-check):

1. `tts.interactive_busy()` is False — nobody is waiting on playback synthesis.
2. **No active-listener prefetch debt:** for every novel whose `progress.updated_at`
   is within the last 90s (frontend heartbeats every ~10s), the current chapter and
   next 3 chapters all have cached audio files. The existing after-play prefetch
   chain fills these; the export merely stays off the worker until they exist.
3. Favorites sync is not mid-run (`library_sync` task not active).

Worst-case interference to a play request = one batch (~5–15s GPU at ~30× realtime).
Export is strictly lowest priority, per user requirement.

## E. API (new `routers/exports.py`)

- `POST /api/novels/{id}/export` `{start_order, end_order, voice, speed}` → job id.
  Validates range against existing chapters; 409 if an identical job is queued/running.
- `GET /api/exports` → active + recent jobs with progress.
- `POST /api/exports/{id}/cancel` (queued or running → canceled; running job stops
  at the next batch boundary; job dir kept).
- `POST /api/exports/{id}/retry` (failed/interrupted/canceled → re-queued, reuses dir).
- `GET /api/plex/libraries` → proxied Plex section list for settings UI.

Deleting a novel cancels its jobs.

## F. Frontend

- Novel page: **Save to Plex** button → modal: From/To chapter inputs (defaults full
  range), voice + speed dropdowns (defaults = novel's effective settings), Export.
  Disabled with hint if `audiobook_dir` is unset.
- Header "Exports" badge visible while a job is queued/running; panel lists jobs with
  progress (chapters done/total, `detail` line), Cancel/Retry. Polls `GET /api/exports`
  every 3s while visible or active. Toast on completion/failure.

## G. Ebook-to-Audiobook CLI

- `--plex` flag: after build, rename output to `"{metadata.title}.m4b"` (sanitized;
  with `--chapters`, append `" - Chapters {min} - {max}"`), move into the Plex folder,
  and if env `PLEX_URL` + `PLEX_TOKEN` are set, trigger the same section refresh
  (section id via env `PLEX_SECTION_ID`; skip refresh if unset).
- `--plex-dir` overrides the folder (default `E:\Plex\Audiobooks\Audiobooks`).
- No other changes to this repo (author-note stripping intentionally not added).

## H. Error handling & risks

- ffmpeg missing → job fails with clear message (already a soft dependency for HLS).
- Disk: ~100MB per audiobook-hour transient in the job dir (PCM_16); assembly briefly
  doubles it. Acceptable on D:.
- Long continuous listening + favorites churn can slow exports arbitrarily — by design.
- Docker/Plex: section refresh avoids container path mapping entirely.

## Out of scope

EPUB file export; non-contiguous chapter selection; reuse of streaming cache;
parallel export worker process (job engine keeps the batch-executor seam so one can
be added later); author-note changes in Ebook-to-Audiobook.

## Testing

- Unit: filename sanitization; batch splitting (paragraph-boundary, ~600 words);
  `_wait_for_export_turn` predicate with faked states; M4B metadata generation.
- Manual E2E: export a short novel while streaming another (verify yielding + no
  playback stalls); verify file lands in Plex folder, Plex rescan fires, Prologue
  sees chapters/cover; kill server mid-job and verify interrupted→retry resumes.
