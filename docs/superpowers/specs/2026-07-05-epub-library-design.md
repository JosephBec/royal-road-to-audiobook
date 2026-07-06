# EPUB Library Folder — Design

**Date:** 2026-07-05
**Status:** Approved

## Purpose

Let the user add EPUB files to the Novel TTS library and listen to them like
any scraped novel. Two entry points, one folder:

- Drag-and-drop an EPUB into an `EPUBs/` folder on disk.
- Upload an EPUB from the mobile web UI.

Primary use case: a Royal Road novel the user follows gets stubbed/published;
they obtain the EPUB and want to keep listening in this app's library — not
as a Plex M4B export. The EPUBs are properly produced books (not scraped
junk), so no special text cleanup is needed.

## Decisions (from brainstorming)

- **Detection:** live folder watching (startup scan + polling loop), so a
  dropped file appears without a restart or button press.
- **Folder location:** `EPUBs/` inside the web app directory
  (`D:\Projects\royal-road-to-audiobook\EPUBs`), auto-created at startup.
- **Source of truth:** the folder. File deleted → book (and its progress)
  removed from the library. Book deleted in the web UI → file deleted from
  the folder.
- **No text cleanup pass** (no patron-credit filtering); read chapters as-is.

## Architecture

The app already routes all chapter text through one interface:
`get_scraper_for_url(url)` → `scrape_chapter_text(url)`. EPUB support plugs
in as a scraper whose "URLs" are pseudo-URLs pointing at local files. All
downstream features (streaming playback, DB text cache, progress, per-novel
settings, favorites prefetch, M4B export) work unchanged because they never
know where text comes from.

### Pseudo-URL scheme

- Novel: `epub://<url-encoded filename>` — e.g. `epub://Mother%20of%20Learning.epub`
- Chapter: `epub://<url-encoded filename>#<chapter-index>` — e.g. `...epub#12`

The filename (unique within the folder) is the novel's identity, matching the
existing `Novel.rr_url` unique constraint. Replacing a file under the same
name keeps identity and progress; renaming counts as delete + re-add and
loses progress (accepted).

## Components

### 1. `scrapers/epub_local.py` — EPUB source

A `BaseScraper` subclass (`name = "epub"`, `url_patterns = [^epub://]`),
auto-discovered by the existing registry. Reads files from the EPUBs folder
instead of making HTTP requests:

- `scrape_novel_metadata(url)` — title, author, description from the EPUB
  OPF metadata; `rr_url` is the canonical `epub://` URL; `cover_url` points
  at the cover endpoint (see component 3).
- `scrape_chapter_list(url)` — chapters from the EPUB spine/TOC:
  `[{title, rr_url (with #index), rr_chapter_id (index), order, published_at: None}]`.
- `scrape_chapter_text(url)` — extract the indexed chapter's HTML and clean
  it to plain text (paragraphs separated by blank lines).

Parsing logic is ported from `Ebook-to-Audiobook/src/epub_parser.py`
(ebooklib + BeautifulSoup: chapter extraction, `clean_html_to_text`,
chapter-title cleanup), dropping the CLI-specific parts (phonemes, M4B).
New dependencies: `ebooklib`, `beautifulsoup4`, `lxml` added to
`requirements.txt`.

Parsing is synchronous file I/O inside async methods; files are local and
parsing runs once per chapter at most (the existing `Chapter.text` DB cache
keeps it from being re-read after first play). Heavy calls are wrapped in
`asyncio.to_thread` to keep the event loop responsive.

### 2. `epub_library.py` — folder sync service

Started from the app lifespan (like `export_worker`). Ensures the `EPUBs/`
folder exists, then runs an asyncio loop:

1. **Startup scan + poll every ~5 s:** list `*.epub` in the folder and diff
   against `Novel` rows with `rr_url LIKE 'epub://%'`.
2. **New file** → skip while its size is still changing (still being
   copied); then parse metadata + chapter list and create the Novel and
   Chapter rows (reusing the same registration flow as `add_novel`),
   extract the cover image to a cache directory.
3. **Changed file** (same name, new mtime/size — a replaced or extended
   edition) → re-parse and re-sync the chapter list via the existing
   `sync_chapter_list`, keeping the novel row and progress.
4. **Missing file** → delete the Novel (cascade removes chapters and
   progress) and remove its cached chapter audio
   (`tts.remove_chapter_audio`).
5. **Unparseable file** → log and skip; never crash the loop. Retry only
   when the file's mtime/size changes.

Extracted covers live in `EPUBs/.covers/` (the sync loop only globs
`*.epub`, so the cache directory is invisible to it).

Exposes `sync_now()` so the upload endpoint can register a book immediately
instead of waiting for the next poll.

### 3. `routers/epubs.py` — upload + cover API

- `POST /api/epubs/upload` — multipart file upload. Validates the `.epub`
  extension and that the payload parses as an EPUB; sanitizes the filename;
  rejects a duplicate filename with 409; writes into the EPUBs folder; calls
  `sync_now()`; returns the created novel (same shape as `add_novel`).
- `GET /api/epubs/{novel_id}/cover` — serves the extracted cover image from
  the cover cache.

### 4. Frontend (`frontend/`)

- **Add Novel dialog:** below the URL input, an "or upload an EPUB" file
  button (`<input type="file" accept=".epub">`) — on iOS this opens the
  Files picker. Shows upload progress, then the book appears in the library
  like any added novel.
- **Library card:** EPUB books show source "epub" (the existing `source`
  field already surfaces the scraper name). Delete works as usual but warns
  that the file will be removed from disk.
- **Refresh:** for EPUB books, refresh re-reads the file (picks up a
  replaced/extended edition under the same filename).

### 5. Deletion from the UI

`DELETE /api/novels/{id}` additionally deletes the underlying `.epub` file
and cached cover when the novel's `rr_url` is an `epub://` URL (before the
row delete, so the sync loop can't race and re-add it).

## Error handling

- Corrupt/invalid EPUB dropped in folder: logged, skipped, no library entry;
  retried only if the file changes.
- Upload of an invalid file: 400 with a clear message; nothing written.
- File deleted mid-listen: chapter text already cached in DB keeps playing;
  the novel disappears from the library on the next sync pass.
- EPUB with no detectable chapters: rejected at registration (log/400), not
  added as an empty book.

## Testing

Following existing `tests/` patterns:

- **EPUB scraper unit tests** against a small generated fixture EPUB:
  metadata, chapter list, chapter text extraction, URL encode/decode
  round-trip (spaces and unicode in filenames).
- **Sync loop tests:** add file → novel appears; remove file → novel +
  progress gone + audio cleanup called; replace file → chapters updated,
  progress kept; partial/corrupt file → skipped without crash.
- **Upload endpoint tests:** happy path, duplicate name, invalid file.
- **Delete endpoint test:** UI delete removes the file from disk.

## Out of scope

- Text cleanup / patron-credit filtering.
- Configurable folder location (can be added to Settings later).
- Non-EPUB formats (MOBI, PDF).
- Automatic Plex/M4B export of EPUB books (manual export still works via
  the existing export flow).
