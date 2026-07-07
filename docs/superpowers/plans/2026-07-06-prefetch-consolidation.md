# Prefetch Consolidation Implementation Plan

**Goal:** Eliminate the duplicate audio rendering caused by two independent prefetch pipelines by (a) making `tts.synthesize_chapter_to_file` de-duplicate concurrent calls for the same chapter, and (b) consolidating both render-ahead pipelines into a single `prefetch.py` worker.

**Root cause being fixed:** `library_sync._sync_novel` (favorites sync) and `routers/chapters.py:_after_synthesis` (per-playback) both render the next chapters with only a check-then-act file-existence guard, so they synthesize the same chapters concurrently — halving GPU throughput and letting playback outrun the queue. (Confirmed in server.log: chapters 3013/3014/3015 each synthesized twice ~1s apart; single active server process.)

**Execution:** Inline, TDD, one commit per task. Run tests: `.venv\Scripts\python.exe -m pytest <path> -v` from repo root.

## Global Constraints
- Repo: D:\Projects\royal-road-to-audiobook, branch: `prefetch-consolidation` off `master`.
- Playback stays a direct fast path (`synthesize_chapter_to_file`); the worker is for render-ahead only.
- Preserve the export-gate behavior: exports yield the GPU while render-ahead is active.
- Preserve `/api/library/sync-status` meaning (favorites chapter-refresh crawl), which the frontend polls to refresh unread counts.
- Per-novel voice must be respected in prefetch (use `effective_settings(novel, settings)["voice"]`).

---

### Task 1: In-flight de-duplication in `tts.synthesize_chapter_to_file`

**Files:** Modify `tts.py`; Test `tests/test_synth_dedup.py`.

The single choke point every caller shares (playback + worker). A per-chapter `asyncio.Lock` plus a re-check inside the lock: concurrent callers for one chapter run the synthesis once; the second awaits and returns the finished file.

- [ ] **Step 1 — failing test** (`tests/test_synth_dedup.py`): monkeypatch the blocking synth (`tts._synthesize_text_blocking`) with a counter that sleeps briefly and returns one short segment; point `tts.TEMP_DIR` at tmp_path; fire two `synthesize_chapter_to_file(same_id, ...)` concurrently via `asyncio.gather`; assert the counter incremented exactly once and both calls returned the same existing path.
- [ ] **Step 2** — run, confirm it fails (counter == 2).
- [ ] **Step 3 — implement**: add a module-level `_synth_locks: dict[int, asyncio.Lock]` (lazily created per chapter id, guarded the same per-event-loop way as elsewhere is unnecessary here since the app is one loop — a plain dict is fine). Wrap the body of `synthesize_chapter_to_file` after the first `exists()` check: acquire the chapter's lock, re-check `output_path.exists()` inside the lock (return early if now present), synthesize, release. Pop the lock when done to avoid unbounded growth.
- [ ] **Step 4** — run, confirm counter == 1, both paths equal.
- [ ] **Step 5** — commit: `fix: de-duplicate concurrent chapter synthesis in tts`

---

### Task 2: `prefetch.py` worker (new single render-ahead pipeline)

**Files:** Create `prefetch.py`; Create `tests/test_prefetch.py`.

**Interface (consumed by Task 3):**
- `start_worker()` — launch the background task (idempotent); called from `main.py` lifespan.
- `enqueue(targets: list[tuple[int, str, str]], voice: str)` — targets are `(chapter_id, rr_url, title)`; skips ids already rendered, in-flight, or already pending.
- `is_busy() -> bool` — True while an item is in-flight or the queue is non-empty (for the export gate).
- `async drain_once()` — process the whole queue once (test seam; the loop calls this).

Behavior: single worker; `_pending: set[int]`; for each target — skip if `tts.temp_path_for_chapter(id).exists()`; `await _wait_for_interactive_idle()` (yield to playback the user waits on, mirroring library_sync); scrape via `get_scraper_for_url(url).scrape_chapter_text(url)`; `await tts.synthesize_chapter_to_file(id, f"{title}\n\n{text}", voice, 1.0)`; on any exception log and continue; remove id from `_pending`. When the queue drains, run retention cleanup: `forever, expiring = retention_policy(db); tts.cleanup_temp_files(forever, expiring)`.

- [ ] **Step 1 — failing tests** (`tests/test_prefetch.py`), using fakes (no GPU/network), following `tests/test_export_worker.py` patterns:
  - `test_enqueue_dedups`: enqueue the same id twice → underlying synth called once.
  - `test_skips_already_rendered`: pre-create the temp file → synth not called for that id.
  - `test_processes_targets`: two distinct ids → both scraped+synthesized in order.
  - `test_runs_retention_cleanup_when_drained`: monkeypatch `retention_policy` + `cleanup_temp_files`, assert cleanup called once after drain.
  Monkeypatch `prefetch.get_scraper_for_url` to a fake with `scrape_chapter_text`, `prefetch.tts.synthesize_chapter_to_file` to a counter, and `_wait_for_interactive_idle` to a no-op.
- [ ] **Step 2** — run, confirm ModuleNotFoundError / failures.
- [ ] **Step 3 — implement** `prefetch.py`.
- [ ] **Step 4** — run, all pass.
- [ ] **Step 5** — commit: `feat: single prefetch worker for render-ahead`

---

### Task 3: Route both pipelines through the worker; wire lifespan + export gate

**Files:** Modify `library_sync.py`, `routers/chapters.py`, `main.py`, `export_worker.py`. Regression across the suite.

- [ ] **Step 1** — `library_sync._sync_novel`: keep step 1 (chapter refresh) and the target computation; replace the step-3 inline synth loop (`library_sync.py:141-154`) with `prefetch.enqueue(target_data, voice)`. Remove now-unused `_wait_for_interactive_idle` if nothing else uses it. Keep `is_running()` (still reflects the crawl).
- [ ] **Step 2** — `routers/chapters.py._after_synthesis`: replace the inline prefetch loop (`chapters.py:239-252`) with `prefetch.enqueue(prefetch_targets, voice)`; drop the now-redundant inline retention cleanup there (worker owns it) — but keep the immediate `cleanup_temp_files(keep_ids | forever, expiring)` guard for the active window if removing it would expose the current chapter; simplest: keep computing `keep_ids` and call `prefetch.enqueue`, then let the worker's drain cleanup run. Verify no test depends on the old inline cleanup (none do).
- [ ] **Step 3** — `main.py` lifespan: `import prefetch; prefetch.start_worker()` after `export_worker.start_worker()`; add `prefetch.stop()`-equivalent after `yield` if the worker needs shutdown (mirror export/epub).
- [ ] **Step 4** — `export_worker.py:64`: change the gate to also yield to prefetch, e.g. `and not library_sync.is_running() and not prefetch.is_busy()`.
- [ ] **Step 5** — full suite: `.venv\Scripts\python.exe -m pytest tests -v`; expect all pass (67 baseline + new prefetch/dedup tests).
- [ ] **Step 6** — commit: `refactor: route favorites-sync and playback prefetch through the worker`

---

### Final: review + verify
- Self-review the diff (concurrency correctness, export-gate signal, retention preserved).
- Manual note for the human: restart the server (tray/manual) to load the new worker; then resume a favorite and confirm the log shows each upcoming chapter synthesized once.
