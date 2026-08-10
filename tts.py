"""
TTS orchestration.

Synthesis itself is delegated to a pluggable engine (see engines/); everything
here is engine-agnostic: temp file management, per-chapter dedup locks, segment
streaming for Instant Play, HLS/AAC encoding and cache retention.

Handles both Mode A (streaming) and Mode B (wait-for-file) playback.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf

import engines
import voice_script

logger = logging.getLogger(__name__)

# Both shipped engines are 24 kHz. Kept as a module constant because callers
# (voice demos, export assembly) import it; use engine.sample_rate when the
# engine is known.
SAMPLE_RATE = 24000
TEMP_DIR = Path("./temp_audio")
TEMP_DIR.mkdir(exist_ok=True)

# Fallback inter-segment silence. The real value is per engine+voice via
# segment_gap_for(); this is only used where no voice context exists.
SEGMENT_GAP_SECONDS = 0.3

# Thread pool for running blocking TTS on a background thread. Single worker:
# one GPU, and serializing keeps VRAM predictable.
_executor = ThreadPoolExecutor(max_workers=1)

# The engine currently loaded on the worker thread. Switching engines unloads
# the previous one so two models never hold VRAM at once.
_active_engine_name: Optional[str] = None
_engine_lock = asyncio.Lock()

# Per-chapter locks so concurrent callers (playback + prefetch worker) never
# synthesize the same chapter twice — the second awaits and reuses the file.
_synth_locks: dict[int, asyncio.Lock] = {}


def get_device() -> str:
    """Detect the best available device."""
    import torch
    if torch.cuda.is_available():
        name = torch.cuda.get_device_name(0)
        vram = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
        logger.info("CUDA available: %s (%.1f GB VRAM)", name, vram)
        return "cuda"
    logger.warning("CUDA not available, falling back to CPU.")
    return "cpu"


async def get_engine(engine_name: str | None = None):
    """Resolve and load an engine, unloading the previous one if it changed."""
    global _active_engine_name
    engine = engines.get_engine(engine_name)
    async with _engine_lock:
        if _active_engine_name != engine.name:
            if _active_engine_name is not None:
                previous = engines.get_engine(_active_engine_name)
                logger.info("Switching TTS engine %s -> %s", previous.name, engine.name)
                await asyncio.get_event_loop().run_in_executor(_executor, previous.unload)
            await asyncio.get_event_loop().run_in_executor(_executor, engine.load)
            _active_engine_name = engine.name
    return engine


def _synthesize_text_blocking(
    engine,
    text: str,
    voice: str,
    speed: float,
) -> list[np.ndarray]:
    """
    Synthesize text to audio segments (blocking, runs in thread pool).
    Returns list of audio numpy arrays.
    """
    return [seg for seg in engine.synthesize(text, voice, speed) if seg is not None and len(seg)]


def segment_gap_for(engine_name: str | None, voice: str) -> float:
    """Silence between segments for a given engine + voice.

    Derived rather than stored so the full WAV, the HLS segment padding and
    the playlist's own duration maths all agree — including after a restart,
    when the in-memory streaming state is gone. A saved playback position has
    to mean the same instant on every one of those timelines.
    """
    return engines.get_engine(engine_name).segment_gap(voice)


def _segments_to_wav_bytes(segments: list[np.ndarray], sample_rate: int = SAMPLE_RATE,
                           gap_seconds: float | list[float] = SEGMENT_GAP_SECONDS) -> bytes:
    """Concatenate audio segments into a single WAV file in memory.

    `gap_seconds` may be one value or one per segment. Per-segment is what
    lets an ellipsis hold a longer beat than a full stop while everything
    else keeps the voice's normal sentence pause.
    """
    if not segments:
        return b""

    gaps = (list(gap_seconds) if isinstance(gap_seconds, (list, tuple))
            else [gap_seconds] * len(segments))
    parts = []
    for i, seg in enumerate(segments):
        parts.append(seg)
        if i < len(segments) - 1:
            width = gaps[i] if i < len(gaps) else SEGMENT_GAP_SECONDS
            parts.append(np.zeros(int(sample_rate * width), dtype=np.float32))

    audio = np.concatenate(parts)
    buf = io.BytesIO()
    sf.write(buf, audio, sample_rate, format="WAV")
    buf.seek(0)
    return buf.read()


def temp_path_for_chapter(chapter_id: int) -> Path:
    """Get the temp file path for a chapter."""
    return TEMP_DIR / f"chapter_{chapter_id}.wav"


# In-memory tracking of streaming synthesis state per chapter
# {chapter_id: {"segments": [duration_float, ...], "complete": bool, "total_duration": float}}
_streaming_state: dict[int, dict] = {}


def _all_temp_audio_files():
    """All temp audio artifacts: full WAVs, segment WAVs, HLS AAC segments."""
    yield from TEMP_DIR.glob("chapter_*.wav")
    yield from TEMP_DIR.glob("chapter_*.aac")
    yield from TEMP_DIR.glob("chapter_*_segments.json")


# How long non-favorite prefetched audio survives (binge cache): 2 days
RETENTION_SECONDS = 2 * 24 * 3600


def cleanup_temp_files(keep_ids: set[int], expiring_ids: set[int] | None = None):
    """
    Remove temp audio files. `keep_ids` are kept unconditionally;
    `expiring_ids` are kept while the file is younger than RETENTION_SECONDS;
    everything else is deleted.
    """
    expiring_ids = expiring_ids or set()
    cutoff = time.time() - RETENTION_SECONDS
    for f in _all_temp_audio_files():
        try:
            # Parse chapter id from "chapter_123.wav" or "chapter_123_seg_0.wav"
            ch_id = int(f.stem.split("_")[1])
        except (ValueError, IndexError):
            continue
        if ch_id in keep_ids:
            continue
        try:
            if ch_id in expiring_ids and f.stat().st_mtime >= cutoff:
                continue
            f.unlink()
            logger.debug("Deleted temp file: %s", f.name)
        except OSError:
            pass
    # Clean streaming state for removed chapters
    for ch_id in list(_streaming_state.keys()):
        if ch_id not in keep_ids and ch_id not in expiring_ids:
            _streaming_state.pop(ch_id, None)


def remove_chapter_audio(chapter_ids: set[int]):
    """Delete cached audio for specific chapters (e.g. after a voice change)."""
    for f in _all_temp_audio_files():
        try:
            ch_id = int(f.stem.split("_")[1])
            if ch_id in chapter_ids:
                f.unlink()
                _streaming_state.pop(ch_id, None)
        except (ValueError, IndexError):
            pass


def cleanup_all_temp_files():
    """Remove ALL temp audio files. Called on server startup/shutdown."""
    count = 0
    for f in _all_temp_audio_files():
        try:
            f.unlink()
            count += 1
        except Exception:
            pass
    _streaming_state.clear()
    if count:
        logger.info("Cleaned up %d temp audio file(s)", count)


async def synthesize_chapter_to_file(
    chapter_id: int,
    text: str,
    voice: str = "af_heart",
    speed: float = 1.0,
    engine_name: str | None = None,
) -> Path:
    """
    Synthesize a full chapter and save to a temp WAV file.
    Returns the path to the file.
    """
    output_path = temp_path_for_chapter(chapter_id)

    # If already synthesized, return immediately
    if output_path.exists():
        logger.info("Chapter %d already synthesized: %s", chapter_id, output_path)
        return output_path

    # Serialize concurrent requests for the same chapter: whoever gets the lock
    # first synthesizes; the rest re-check and reuse the finished file.
    lock = _synth_locks.setdefault(chapter_id, asyncio.Lock())
    async with lock:
        try:
            if output_path.exists():
                logger.info("Chapter %d already synthesized: %s", chapter_id, output_path)
                return output_path

            engine = await get_engine(engine_name)
            logger.info("Synthesizing chapter %d to file (engine=%s, voice=%s, speed=%.1f)",
                        chapter_id, engine.name, voice, speed)
            start = time.time()

            loop = asyncio.get_event_loop()
            segments = await loop.run_in_executor(
                _executor,
                _synthesize_text_blocking, engine, text, voice, speed
            )

            gap = engine.segment_gap(voice)
            wav_bytes = _segments_to_wav_bytes(segments, engine.sample_rate, gap)
            output_path.write_bytes(wav_bytes)

            elapsed = time.time() - start
            duration = sum(len(s) for s in segments) / engine.sample_rate
            logger.info(
                "Chapter %d synthesized: %.1fs audio in %.1fs (%.1fx realtime)",
                chapter_id, duration, elapsed, duration / elapsed if elapsed > 0 else 0
            )

            return output_path
        finally:
            # Safe to drop: the file now exists, so any waiter re-checks and
            # returns, and later callers short-circuit on the top-level
            # exists() guard before ever touching the lock. Bounds dict growth.
            _synth_locks.pop(chapter_id, None)


async def synthesize_batch(
    text: str, voice: str, speed: float, engine_name: str | None = None
) -> list[np.ndarray]:
    """Synthesize one export batch on the shared TTS worker.

    Deliberately one small executor job: exports call this per ~600-word
    batch and yield between calls, keeping worst-case playback latency to
    a single batch. (A future parallel export lane replaces this seam.)

    Engines that can't vary their own rate (engine.supports_speed False) render
    at natural pace here; export_worker time-stretches the result instead.
    """
    engine = await get_engine(engine_name)
    synth_speed = speed if engine.supports_speed else 1.0
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor, _synthesize_text_blocking, engine, text, voice, synth_speed
    )


# ===== Segment-based streaming for Instant Play mode =====


def get_streaming_state(chapter_id: int) -> dict | None:
    """Get the current streaming synthesis state for a chapter."""
    return _streaming_state.get(chapter_id)


def _segment_path(chapter_id: int, index: int) -> Path:
    """Path for an individual segment WAV file."""
    return TEMP_DIR / f"chapter_{chapter_id}_seg_{index}.wav"


def _aac_segment_path(chapter_id: int, index: int) -> Path:
    """Path for an individual HLS AAC segment file."""
    return TEMP_DIR / f"chapter_{chapter_id}_seg_{index}.aac"


def _segment_index_path(chapter_id: int) -> Path:
    """Sidecar holding each segment's measured duration plus a fingerprint of
    the inputs that produced them."""
    return TEMP_DIR / f"chapter_{chapter_id}_segments.json"


def render_fingerprint(text: str, engine_name: str, voice: str, chunk_count: int) -> dict:
    """Identity of a render, so partial work is only resumed when it still applies.

    The sentence pause is deliberately absent: it is applied when segments are
    assembled, not baked into them, so changing it does not invalidate audio
    already on disk. Everything else here does — a re-scrape changes the
    sentences, and a different engine or voice changes who is speaking them.
    """
    import hashlib
    return {
        "text": hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16],
        "engine": engine_name or "",
        "voice": voice or "",
        "chunks": chunk_count,
    }


def _read_sidecar(chapter_id: int) -> dict:
    import json
    path = _segment_index_path(chapter_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    if isinstance(data, list):
        # Pre-fingerprint format: durations only. Readable for playlist
        # purposes but never resumable, since we can't tell what produced it.
        return {"durations": data}
    return data if isinstance(data, dict) else {}


def _write_sidecar(chapter_id: int, data: dict):
    import json
    try:
        _segment_index_path(chapter_id).write_text(json.dumps(data))
    except OSError:
        pass


def record_segment_duration(chapter_id: int, index: int, duration: float,
                            fingerprint: dict | None = None):
    """Record a segment's real length (and optionally the render fingerprint).

    The playlist used to recompute this as `wav duration + whatever gap the
    voice is set to now`, which breaks as soon as the pause changes: audio
    already on disk was encoded with the old value. Over a few hundred segments
    a 0.4s discrepancy compounds into minutes and resume lands in the wrong
    place. Record the value used at encode time instead.

    Note this is computed, not probed: ADTS AAC carries no duration header, so
    ffprobe only estimates it from bitrate — unreliable to a few hundred ms.
    The WAV length plus the pad we passed to ffmpeg is exact.
    """
    data = _read_sidecar(chapter_id)
    durations = data.get("durations")
    if not isinstance(durations, list):
        durations = []
    while len(durations) <= index:
        durations.append(None)
    durations[index] = round(duration, 4)
    data["durations"] = durations
    if fingerprint is not None:
        data["fingerprint"] = fingerprint
    _write_sidecar(chapter_id, data)


def segment_durations(chapter_id: int) -> list[float] | None:
    """Measured segment durations, or None if unusable/incomplete."""
    durations = _read_sidecar(chapter_id).get("durations")
    if not isinstance(durations, list) or not durations or any(d is None for d in durations):
        return None
    return [float(d) for d in durations]


def resumable_segment_count(chapter_id: int, fingerprint: dict) -> int:
    """How many leading segments of a partial render can be reused.

    Derived from the files actually on disk rather than a stored counter: a
    counter can desync from reality when retention sweeps a chapter or a write
    fails halfway, and then it points at work that isn't there. The filesystem
    cannot be wrong about which segments exist.

    The highest segment is deliberately discarded. If the process died
    mid-write it is a truncated WAV, and re-rendering one sentence is cheaper
    than detecting corruption.
    """
    if _read_sidecar(chapter_id).get("fingerprint") != fingerprint:
        return 0  # different text, engine or voice — the audio is not ours

    index = 0
    while _segment_path(chapter_id, index).exists():
        index += 1
    return max(0, index - 1)


def _existing_segment_indices(chapter_id: int) -> list[int]:
    """Every segment index with an artifact on disk, gaps included.

    Globbed rather than counted upward: the set is not necessarily a prefix.
    Retention sweeps and interrupted renders both leave holes, and a scan that
    stops at the first hole cannot see what is past it.
    """
    prefix = f"chapter_{chapter_id}_seg_"
    indices = set()
    for pattern in (f"{prefix}*.wav", f"{prefix}*.aac"):
        for path in TEMP_DIR.glob(pattern):
            try:
                indices.add(int(path.stem[len(prefix):]))
            except ValueError:
                continue
    return sorted(indices)


def discard_segments_from(chapter_id: int, start_index: int):
    """Remove segment artifacts at or beyond an index (stale tail of a render).

    Every index at or past the mark goes, not just the run starting there.
    Segments surviving behind a gap belong to a render that no longer matches
    the one about to be written, and the next render only overwrites the
    indices it actually produces — a shorter one would leave the old tail in
    place to be served as the end of the new chapter.
    """
    for index in _existing_segment_indices(chapter_id):
        if index < start_index:
            continue
        _segment_path(chapter_id, index).unlink(missing_ok=True)
        _aac_segment_path(chapter_id, index).unlink(missing_ok=True)

    # Keep the sidecar honest about what exists. A duration left behind for a
    # deleted segment is what makes a playlist advertise audio that 404s.
    data = _read_sidecar(chapter_id)
    durations = data.get("durations")
    if isinstance(durations, list) and len(durations) > start_index:
        data["durations"] = durations[:start_index]
        _write_sidecar(chapter_id, data)


def _recorded_gap(chapter_id: int, index: int, fallback: float) -> float:
    """The gap a already-rendered segment was encoded with.

    Recovered from its recorded duration minus the audio's own length, so a
    resumed render rebuilds the full file with the same spacing it streamed
    with — otherwise an ellipsis pause would differ between the two timelines.
    """
    durations = _read_sidecar(chapter_id).get("durations") or []
    if index < len(durations) and durations[index] is not None:
        try:
            speech = sf.info(str(_segment_path(chapter_id, index))).duration
            return max(0.0, float(durations[index]) - speech)
        except Exception:
            pass
    return fallback


def _encode_segment_aac(chapter_id: int, index: int,
                        gap_seconds: float = SEGMENT_GAP_SECONDS,
                        fingerprint: dict | None = None) -> bool:
    """
    Encode a WAV segment to packed ADTS AAC for native HLS playback (iOS).
    Returns False (and logs) if ffmpeg is unavailable or fails — the WAV
    segment fallback still works in that case.

    The trailing pad must match the silence baked into the concatenated full
    file (see _segments_to_wav_bytes), or the HLS and full-file timelines drift
    apart and a saved position resumes in the wrong place.
    """
    wav = _segment_path(chapter_id, index)
    aac = _aac_segment_path(chapter_id, index)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(wav),
        "-af", f"apad=pad_dur={gap_seconds}",
        "-c:a", "aac", "-b:a", "96k",
        str(aac),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60,
                       creationflags=creationflags)
    except Exception as e:
        logger.warning("AAC encode failed for chapter %d seg %d: %s", chapter_id, index, e)
        return False
    try:
        record_segment_duration(chapter_id, index,
                                sf.info(str(wav)).duration + gap_seconds,
                                fingerprint)
    except Exception:
        logger.warning("Could not record duration for chapter %d seg %d",
                       chapter_id, index)
    return True


def _save_segment_wav(chapter_id: int, index: int, audio: np.ndarray,
                      sample_rate: int = SAMPLE_RATE) -> float:
    """Save a single segment as a WAV file. Returns duration in seconds."""
    path = _segment_path(chapter_id, index)
    buf = io.BytesIO()
    sf.write(buf, audio, sample_rate, format="WAV")
    path.write_bytes(buf.getvalue())
    return len(audio) / sample_rate


async def synthesize_chapter_streaming(*args, **kwargs):
    """Serialize streaming renders per chapter, then delegate.

    Two concurrent renders of the same chapter used to merely duplicate work.
    Now that partial renders are resumable each one computes a resume point and
    discards the tail beyond it, so a second caller actively deletes the first
    caller's segments — playback and the head-start pass were destroying each
    other's progress. The file-based path has always taken this lock; the
    streaming path needs it for the same reason and more urgently.
    """
    chapter_id = kwargs.get("chapter_id", args[0] if args else None)
    lock = _synth_locks.setdefault(chapter_id, asyncio.Lock())
    async with lock:
        try:
            return await _synthesize_chapter_streaming(*args, **kwargs)
        finally:
            # Safe to drop: a later caller re-checks the completed file and the
            # on-disk segments, so nothing depends on the lock outliving this.
            _synth_locks.pop(chapter_id, None)


async def _synthesize_chapter_streaming(
    chapter_id: int,
    text: str,
    voice: str = "af_heart",
    speed: float = 1.0,
    engine_name: str | None = None,
    chunk_voices: list[str] | None = None,
    max_seconds: float | None = None,
    yield_to_interactive: bool = False,
):
    """
    Synthesize a chapter segment by segment for Instant Play.
    Each segment is saved as its own WAV file. State is tracked in _streaming_state.
    When complete, also saves the full concatenated file.

    `chunk_voices` optionally gives a voice per chunk, letting a chapter render
    dialogue in per-character voices (see voice_script). It must line up with
    engine.plan_chunks(text); anything missing falls back to `voice`. Cloning
    engines cache each voice's conditionals, so switching between them per
    sentence costs a dict lookup after the first use.

    `max_seconds` stops once that much audio exists, leaving a resumable
    partial render — this is how a chapter gets a head start so playback can
    begin while the rest is still being produced.

    A render interrupted for any reason (restart, crash) resumes from the
    segments already on disk rather than starting over.

    `yield_to_interactive` makes a background render stand aside between
    chunks whenever the user is waiting on something. Checking only before a
    chapter starts is not enough — one chapter is minutes of work on
    Chatterbox, and pressing play in the middle of it meant waiting it out.
    """
    # Check if already fully synthesized
    if temp_path_for_chapter(chapter_id).exists():
        info = sf.info(str(temp_path_for_chapter(chapter_id)))
        _streaming_state[chapter_id] = {
            "segments": [], "complete": True,
            "total_duration": info.duration, "file_ready": True,
        }
        return

    engine = await get_engine(engine_name)
    gap = engine.segment_gap(voice)
    loop = asyncio.get_event_loop()
    chunks = engine.plan_chunks(text)
    fingerprint = render_fingerprint(text, engine.name, voice, len(chunks))

    # Reuse a partial render if one is on disk and was produced from the same
    # text, engine and voice. Chunking is deterministic, so segment N is always
    # the same sentence — the work already paid for is still good.
    resume_from = resumable_segment_count(chapter_id, fingerprint)
    discard_segments_from(chapter_id, resume_from)

    all_segments: list[np.ndarray] = []
    segment_gaps: list[float] = []
    resumed_duration = 0.0
    for index in range(resume_from):
        try:
            audio, _sr = sf.read(str(_segment_path(chapter_id, index)), dtype="float32")
        except Exception:
            # Unreadable segment: everything from here on is suspect.
            discard_segments_from(chapter_id, index)
            all_segments = all_segments[:index]
            resume_from = index
            break
        all_segments.append(audio)
        segment_gaps.append(_recorded_gap(chapter_id, index, gap))
        resumed_duration += len(audio) / engine.sample_rate
    if resume_from:
        logger.info("Chapter %d resuming at chunk %d/%d (%.0fs already rendered)",
                    chapter_id, resume_from, len(chunks), resumed_duration)

    _streaming_state[chapter_id] = {
        "segments": [len(a) / engine.sample_rate for a in all_segments],
        "complete": False,
        "total_duration": resumed_duration,
        "file_ready": False,
    }

    def _render_chunk(chunk_text: str, chunk_voice: str) -> list[np.ndarray]:
        return [a for a in engine.synthesize(chunk_text, chunk_voice, speed)
                if a is not None and len(a) > 0]

    # One executor submission per chunk rather than one for the whole chapter.
    # The pool has a single worker and runs FIFO, so submitting the next chunk
    # only after the previous finishes leaves a gap where interactive work — a
    # voice demo, or the chapter the user just pressed play on — can take the
    # worker. Rendering the chapter as one job made those wait out the entire
    # render, which on Chatterbox is many minutes.
    seg_index = resume_from
    stopped_early = False
    try:
        for chunk_number in range(resume_from, len(chunks)):
            if max_seconds is not None and                     _streaming_state[chapter_id]["total_duration"] >= max_seconds:
                stopped_early = True
                break
            while yield_to_interactive and interactive_busy():
                await asyncio.sleep(1)
            chunk_voice = voice
            if chunk_voices and chunk_number < len(chunk_voices):
                chunk_voice = chunk_voices[chunk_number] or voice
            # An ellipsis gets a longer pause after it than a full stop does.
            chunk_gap = gap * voice_script.gap_multiplier(chunks[chunk_number])
            produced = await loop.run_in_executor(
                _executor, _render_chunk, chunks[chunk_number], chunk_voice)
            for position, audio in enumerate(produced):
                # Only the chunk's final segment carries its gap; any earlier
                # ones are mid-chunk and keep the ordinary pause.
                this_gap = chunk_gap if position == len(produced) - 1 else gap
                all_segments.append(audio)
                segment_gaps.append(this_gap)
                dur = _save_segment_wav(chapter_id, seg_index, audio, engine.sample_rate)
                _encode_segment_aac(chapter_id, seg_index, this_gap, fingerprint)
                st = _streaming_state.get(chapter_id)
                if st is not None:
                    st["segments"].append(dur)
                    st["total_duration"] += dur
                seg_index += 1
    except Exception as e:
        logger.error("Streaming synthesis error for chapter %d: %s", chapter_id, e)

    if stopped_early:
        # A head start, not a finished chapter: leave the segments on disk for
        # a later call to resume from, and write no full file — its existence
        # is what marks a chapter complete.
        logger.info("Chapter %d head start: %d chunk(s), %.0fs of audio",
                    chapter_id, seg_index, _streaming_state[chapter_id]["total_duration"])
        return

    # Save complete concatenated file
    if all_segments:
        wav_bytes = _segments_to_wav_bytes(all_segments, engine.sample_rate, segment_gaps)
        temp_path_for_chapter(chapter_id).write_bytes(wav_bytes)

    st = _streaming_state.get(chapter_id)
    if st is not None:
        st["complete"] = True
        st["file_ready"] = True

    logger.info("Streaming synthesis complete for chapter %d: %d segments, %.1fs total",
                chapter_id, len(all_segments),
                st["total_duration"] if st else 0)


def get_chapter_status(chapter_id: int) -> dict:
    """Check if a chapter's audio file is ready and its duration."""
    path = temp_path_for_chapter(chapter_id)
    if path.exists():
        try:
            info = sf.info(str(path))
            return {"ready": True, "duration_seconds": info.duration}
        except Exception:
            return {"ready": True, "duration_seconds": None}
    return {"ready": False, "duration_seconds": None}


# ===== Interactive-synthesis tracking =====
# Background pipelines (favorites sync, playback prefetch) yield to synthesis
# the user is actively waiting on.

_interactive_count = 0


@contextmanager
def interactive_synthesis():
    """Mark a synthesis the user is waiting on (playback request)."""
    global _interactive_count
    _interactive_count += 1
    try:
        yield
    finally:
        _interactive_count -= 1


def interactive_busy() -> bool:
    return _interactive_count > 0
