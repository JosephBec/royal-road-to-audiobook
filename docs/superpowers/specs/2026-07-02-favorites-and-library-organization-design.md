# Favorites, Auto-Download Pipeline & Library Organization — Design

**Date:** 2026-07-02
**Status:** Approved in conversation

## Concept

Favorites are actively-followed novels: auto-refreshed and pre-downloaded so new
chapters are always ready. Non-favorites are binge reads: cached generously
while being read, but expiring after 2 days.

## 1. Schema (novels table, migrated via existing `_migrate_schema`)

- `favorite` BOOLEAN NOT NULL DEFAULT 0
- `sort_order` INTEGER NULL (manual card order; NULL = unordered)

"Recently listened" needs no column — it derives from `progress.updated_at`.

## 2. API

- `PATCH /api/novels/{id}/settings` gains `favorite: bool | None` (same
  provided-fields semantics as other overrides).
- `NovelResponse` gains `favorite`, `sort_order`, `progress_updated_at`.
- `PUT /api/novels/order` body `{"ids": [novel ids in display order]}` →
  assigns `sort_order` 0..n. 400 on unknown ids.
- `POST /api/library/refresh-favorites` → kicks a background pipeline (below);
  server-side cooldown 10 minutes (returns `{"started": false, "cooldown_remaining": s}`
  when throttled). Fire-and-forget from the frontend on every page load.

## 3. Background favorites pipeline (new module `library_sync.py`)

For each favorite novel, sequentially (single task; rate-limited scraping):
1. Re-crawl the chapter list; insert new chapters (reuses the refresh logic,
   extracted into a shared helper from `refresh_novel`).
2. From the novel's progress chapter (or chapter 1 if no progress), take the
   next 3 chapters; for each with no cached audio: scrape text, prepend title,
   `synthesize_chapter_to_file`.
3. Runs retention cleanup at the end.

**Yielding to interactive playback:** tts.py tracks an interactive-synthesis
counter (incremented by the /synthesize and /stream endpoints' work). The
pipeline checks it between chapters and waits while nonzero, so hitting play
on an uncached chapter only ever waits behind at most the chapter currently
being synthesized.

## 4. Prefetch depth during playback

`_after_synthesis` (chapters router) now walks all 3 `next_chapters` (not just
the first) and synthesizes each missing one sequentially — favorites and
non-favorites alike. Replaces single-slot `prefetch_next_chapter`.

## 5. Retention policy (replaces keep-set cleanup)

Computed from DB by `retention_policy(db)` → `(forever_ids, expiring_ids)`:

- **forever:** every novel's in-progress chapter; plus next-3-from-progress for
  favorites.
- **expiring (2 days):** next-3-from-progress for non-favorites. Deleted when
  file mtime > 48h.
- everything else: deleted at next cleanup.

`cleanup_temp_files(forever_ids, expiring_ids)` applies it. Called after
synthesis, at pipeline end, and on server start/stop. The currently playing
chapter is protected because progress is saved as soon as playback starts.

## 6. Auto-refresh scope

- Site load → `refresh-favorites` (all favorites, cooldown-limited).
- `openNovel` auto-crawl now happens **only for favorites**; non-favorites use
  the manual ↻ Refresh button.

## 7. Frontend: navigation & organization

- **Tabs:** All / ⭐ Favorites above the grid. Hash-routed: `/#favorites`
  opens the Favorites tab directly — bookmarkable on the phone.
- **Star toggle** on each card (corner) and on the novel detail header.
- **Sort menu** (persisted in localStorage): Recently Listened (progress
  updated_at desc) / Recently Added (created_at desc, current behavior) /
  Title A–Z / Custom (sort_order asc, NULLs last). In the All tab, favorites
  group first under any sort.
- **Drag-to-reorder:** pointer-events long-press (~400 ms) drag on cards,
  works with touch on iOS; on drop, saves order via `PUT /api/novels/order`
  and switches the sort menu to Custom.

## Testing

TestClient: favorite PATCH + response fields, order endpoint, retention_policy
sets (favorite vs non-favorite vs stale mtime), refresh-favorites cooldown,
pipeline behavior with stubbed scraper/TTS (correct chapters queued, skips
cached, yields to interactive flag). Manual: bookmark `/#favorites`, star
toggles, drag on phone, sort persistence.
