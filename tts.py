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
                           gap_seconds: float = SEGMENT_GAP_SECONDS) -> bytes:
    """Concatenate audio segments into a single WAV file in memory."""
    if not segments:
        return b""

    silence = np.zeros(int(sample_rate * gap_seconds), dtype=np.float32)
    parts = []
    for i, seg in enumerate(segments):
        parts.append(seg)
        if i < len(segments) - 1:
            parts.append(silence)

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
    """Sidecar recording each AAC segment's real, measured duration."""
    return TEMP_DIR / f"chapter_{chapter_id}_segments.json"


def record_segment_duration(chapter_id: int, index: int, duration: float):
    """Append a measured segment duration to the chapter's sidecar.

    The playlist used to recompute this as `wav duration + whatever gap the
    voice is set to now`, which breaks as soon as the pause changes: audio
    already on disk was encoded with the old value. Over a few hundred segments
    a 0.4s discrepancy compounds into minutes and resume lands in the wrong
    place. Record the value used at encode time instead.

    Note this is computed, not probed: ADTS AAC carries no duration header, so
    ffprobe only estimates it from bitrate — unreliable to a few hundred ms.
    The WAV length plus the pad we passed to ffmpeg is exact.
    """
    import json
    path = _segment_index_path(chapter_id)
    try:
        durations = json.loads(path.read_text()) if path.exists() else []
        if not isinstance(durations, list):
            durations = []
    except Exception:
        durations = []
    while len(durations) <= index:
        durations.append(None)
    durations[index] = round(duration, 4)
    try:
        path.write_text(json.dumps(durations))
    except OSError:
        pass


def segment_durations(chapter_id: int) -> list[float] | None:
    """Measured segment durations, or None if this chapter predates the sidecar."""
    import json
    path = _segment_index_path(chapter_id)
    if not path.exists():
        return None
    try:
        durations = json.loads(path.read_text())
    except Exception:
        return None
    if not isinstance(durations, list) or any(d is None for d in durations):
        return None
    return [float(d) for d in durations]


def _encode_segment_aac(chapter_id: int, index: int,
                        gap_seconds: float = SEGMENT_GAP_SECONDS) -> bool:
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
                                sf.info(str(wav)).duration + gap_seconds)
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


async def synthesize_chapter_streaming(
    chapter_id: int,
    text: str,
    voice: str = "af_heart",
    speed: float = 1.0,
    engine_name: str | None = None,
):
    """
    Synthesize a chapter segment by segment for Instant Play.
    Each segment is saved as its own WAV file. State is tracked in _streaming_state.
    When complete, also saves the full concatenated file.
    """
    # Check if already fully synthesized
    if temp_path_for_chapter(chapter_id).exists():
        info = sf.info(str(temp_path_for_chapter(chapter_id)))
        _streaming_state[chapter_id] = {
            "segments": [], "complete": True,
            "total_duration": info.duration, "file_ready": True,
        }
        return

    _streaming_state[chapter_id] = {
        "segments": [], "complete": False,
        "total_duration": 0.0, "file_ready": False,
    }

    engine = await get_engine(engine_name)
    gap = engine.segment_gap(voice)
    loop = asyncio.get_event_loop()
    all_segments: list[np.ndarray] = []

    def _render_chunk(chunk_text: str) -> list[np.ndarray]:
        return [a for a in engine.synthesize(chunk_text, voice, speed)
                if a is not None and len(a) > 0]

    # One executor submission per chunk rather than one for the whole chapter.
    # The pool has a single worker and runs FIFO, so submitting the next chunk
    # only after the previous finishes leaves a gap where interactive work — a
    # voice demo, or the chapter the user just pressed play on — can take the
    # worker. Rendering the chapter as one job made those wait out the entire
    # render, which on Chatterbox is many minutes.
    seg_index = 0
    try:
        for chunk_text in engine.plan_chunks(text):
            for audio in await loop.run_in_executor(_executor, _render_chunk, chunk_text):
                all_segments.append(audio)
                dur = _save_segment_wav(chapter_id, seg_index, audio, engine.sample_rate)
                _encode_segment_aac(chapter_id, seg_index, gap)
                st = _streaming_state.get(chapter_id)
                if st is not None:
                    st["segments"].append(dur)
                    st["total_duration"] += dur
                seg_index += 1
    except Exception as e:
        logger.error("Streaming synthesis error for chapter %d: %s", chapter_id, e)

    # Save complete concatenated file
    if all_segments:
        wav_bytes = _segments_to_wav_bytes(all_segments, engine.sample_rate, gap)
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
