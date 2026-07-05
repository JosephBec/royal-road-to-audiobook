# Library UI: Full Covers, List View, Desktop Drag — Design

**Date:** 2026-07-05 · **Scope:** frontend only (`frontend/app.js`, `frontend/index.html`, `frontend/style.css`)

## Problem

1. Grid cards crop portrait covers into a fixed 280×180 landscape box (`object-fit: cover`), hiding ~2/3 of the art.
2. No list view.
3. Drag-to-reorder requires a 400ms long-press (mobile-tuned); awkward with a mouse.

## Decisions (settled with user)

1. **Book-style grid cards**: `.novel-card-cover` uses `aspect-ratio: 2/3` (auto height) instead of fixed height; grid columns `minmax(180px, 1fr)` instead of `minmax(280px, 1fr)`; the mobile breakpoint's `height: 120px` override is replaced by the same aspect-ratio treatment. Slight edge-crop for non-2:3 covers is accepted (chosen over letterbox and full-bleed poster).
2. **Compact list view**: grid/list toggle button next to the library sort dropdown; persisted as `localStorage.libraryView` (`grid` default). Rows ≈56px: 2:3 thumbnail (~37×56), one-line ellipsized title, author, right-aligned progress/resume badge, favorite star, hover delete. Rows keep the `novel-card` class (plus a `novel-card--row` modifier) and the same `data-id`/handlers so click-to-open, resume, favorite, delete, and drag-reorder work unchanged in both views. `renderLibrary()` branches on the view for markup only.
3. **Desktop drag**: in `setupCardDrag`, `pointerType === 'mouse'` skips the long-press timer entirely — press + move >8px starts the drag immediately; release without movement remains a click. Text selection suppressed while dragging. Touch/pen keep the existing 400ms long-press. Same behavior in list view via the shared class.

## Out of scope

Backend/API/DB changes; comfortable (description) list rows; poster cards.

## Verification

`node --check frontend/app.js`; server smoke (new toggle/element IDs served); python suite unchanged; visual pass by the user (no JS test rig exists).
