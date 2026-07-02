# iOS Playback Fixes Round 2 — Design

**Date:** 2026-07-02
**Status:** Approved in conversation (user requested all items)

## 1. Lock-screen skip amounts (iOS)

Research (dbushell.com, web.dev): the web page controls the actual seek amount;
iOS draws its own icon (often "10") regardless. Current code obeys
`details.seekOffset || 15`, and iOS suggests 10 — hence 10s skips.
**Fix:** hard-code `seekRelative(-15)` / `seekRelative(30)` in the
seekbackward/seekforward handlers, matching the in-app buttons. The icon may
still read 10; the behavior will be 15/30.

## 2. Lock-screen play after pause (iOS)

Root cause: we never set `navigator.mediaSession.playbackState`, so iOS
releases the Now Playing session once paused; the lock-screen play button then
has nothing to command.
**Fix:**
- Explicit `play`/`pause` action handlers that call `audio.play()`/`audio.pause()`
  directly (no toggle — toggles desync).
- Sync `state.isPlaying`, the play button glyph, and
  `navigator.mediaSession.playbackState` from the audio element's `play`/`pause`
  events (single source of truth). Pause handler skips the glyph change while
  `isSynthesizing` so the ⏳ spinner isn't overwritten.
- `setPositionState({duration, playbackRate, position})` on loadedmetadata,
  durationchange, seeked, ratechange, and play (guarded to finite durations) so
  the lock-screen scrubber is accurate.
- `togglePlayPause` becomes `audio.paused ? play() : pause()`.

## 3. Pagination page-select

The "…" buttons are disabled placeholders. Replace the numbered-button window
with: `‹ Prev  [Page N of M ▾]  Next ›` where the middle is a native `<select>`
(opens the iOS wheel picker). Styled like the buttons.

## 4. Stale frontend cache on phones

Safari heuristically caches `/static/app.js` etc., so shipped fixes don't
arrive (this is why the ⚙ Novel button appeared missing). Add an HTTP
middleware in main.py setting `Cache-Control: no-cache` on `/`, `/static/*`,
and `*.m3u8` (ETag revalidation still makes repeat loads cheap on LAN).

## 5. Chapter title spoken at chapter start

Prepend the chapter title to the synthesized text (`f"{title}\n\n{text}"`) at
all three scrape-for-synthesis sites: stream endpoint, synthesize endpoint,
and the prefetch step (capture the next chapter's title alongside its URL).
Already-cached audio is unaffected until re-synthesized.

## 6. Seamless Instant Play via native HLS (iPhone)

Current instant mode plays segment WAVs back-to-back with an audible gap
(network fetch + canplaythrough wait between clips).

Research: iPhone Safari plays HLS natively in `<audio>`, including *growing*
EVENT playlists (live-radio style) — seamless transitions, background/lock
screen playback, Media Session all work. MSE/ManagedMediaSource needs iOS
17.1+ and more machinery; Web Audio scheduling dies when the screen locks.
**HLS is the fit.** ffmpeg 4.4 is installed on the host.

- **Backend (tts.py):** after each streaming segment WAV is saved, encode a
  packed-audio ADTS AAC copy via ffmpeg (`-af apad=pad_dur=0.3 -c:a aac -b:a 96k`)
  → `chapter_{id}_seg_{n}.aac`. The 0.3 s tail pad matches the inter-segment
  silence baked into the concatenated full WAV, keeping the two timelines
  aligned so saved positions transfer.
- **Routes (chapters.py):**
  - `GET /api/chapters/{id}/hls.m3u8` — builds an EVENT playlist from the .aac
    files on disk (`#EXT-X-PLAYLIST-TYPE:EVENT`, EXTINF = wav duration + 0.3,
    absolute segment URIs, `#EXT-X-ENDLIST` appended once synthesis is
    complete). `Cache-Control: no-cache`.
  - `GET /api/chapters/{id}/hls/{n}.aac` — serves a segment (`audio/aac`).
  - `/segments` response gains `aac_count` so the client knows when HLS can start.
- **Frontend:** `playInstant` dispatches on
  `audio.canPlayType('application/vnd.apple.mpegurl')`:
  - Native HLS (Safari/iOS): wait for the first .aac segment, then
    `audio.src = /api/chapters/{id}/hls.m3u8` and play. While the playlist is
    live, duration is Infinity → show elapsed time, keep scrubbar hidden; on
    `durationchange` to a finite value (ENDLIST reached) show scrubbar +
    duration. No mid-play swap to the full file is needed — the finished
    playlist IS the full chapter. `ended` → autoplay works natively.
  - Other browsers (desktop Chrome): keep the existing segment-loop fallback
    unchanged.
- **Cleanup:** temp-file cleanup also removes `chapter_*.aac`. If ffmpeg fails,
  log and continue — WAV segments and the fallback loop still work.
- **Dead code:** remove unused `cleanup_stale_temp_files`.

## Testing

TestClient: playlist endpoint shape (EVENT header, EXTINF entries, ENDLIST when
complete), aac_count in /segments, title-prepend in synthesized text (stubbed
TTS). ffmpeg encode smoke-tested with a generated WAV. Manual on iPhone:
lock-screen resume, 15/30 skips, gapless instant play, page picker.
