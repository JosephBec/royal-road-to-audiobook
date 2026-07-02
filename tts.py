"""
TTS Engine Wrapper

Kokoro TTS synthesis with streaming support and temp file management.
Handles both Mode A (streaming) and Mode B (wait-for-file) playback.
"""

import asyncio
import io
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
import torch
from kokoro import KPipeline

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
TEMP_DIR = Path("./temp_audio")
TEMP_DIR.mkdir(exist_ok=True)

# Thread pool for running blocking TTS on a background thread
_executor = ThreadPoolExecutor(max_workers=1)

# Track the TTS pipeline singleton
_pipeline: Optional[KPipeline] = None
_pipeline_lock = asyncio.Lock()
_pipeline_device: Optional[str] = None


def get_device() -> str:
    """Detect the best available device."""
    if torch.cuda.is_available():
        name = torch.cuda.get_device_name(0)
        vram = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
        logger.info("CUDA available: %s (%.1f GB VRAM)", name, vram)
        return "cuda"
    logger.warning("CUDA not available, falling back to CPU.")
    return "cpu"


async def get_pipeline(voice: str = "af_heart") -> KPipeline:
    """Get or create the Kokoro pipeline singleton."""
    global _pipeline, _pipeline_device
    async with _pipeline_lock:
        if _pipeline is None:
            device = get_device()
            _pipeline_device = device
            logger.info("Initializing Kokoro pipeline (device=%s)", device)
            # Initialize in thread pool since it downloads/loads model weights
            loop = asyncio.get_event_loop()
            _pipeline = await loop.run_in_executor(
                _executor,
                lambda: KPipeline(lang_code="a", device=device)
            )
            logger.info("Kokoro pipeline initialized.")
        return _pipeline


def _synthesize_text_blocking(
    pipeline: KPipeline,
    text: str,
    voice: str,
    speed: float,
) -> list[np.ndarray]:
    """
    Synthesize text to audio segments (blocking, runs in thread pool).
    Returns list of audio numpy arrays.
    """
    segments = []
    generator = pipeline(text, voice=voice, speed=speed, split_pattern=r'\n+')
    for graphemes, phonemes, audio in generator:
        if audio is not None and len(audio) > 0:
            segments.append(audio)
    return segments


def _segments_to_wav_bytes(segments: list[np.ndarray]) -> bytes:
    """Concatenate audio segments into a single WAV file in memory."""
    if not segments:
        return b""

    silence = np.zeros(int(SAMPLE_RATE * 0.3), dtype=np.float32)
    parts = []
    for i, seg in enumerate(segments):
        parts.append(seg)
        if i < len(segments) - 1:
            parts.append(silence)

    audio = np.concatenate(parts)
    buf = io.BytesIO()
    sf.write(buf, audio, SAMPLE_RATE, format="WAV")
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


def cleanup_temp_files(keep_ids: set[int]):
    """Remove temp audio files except for the given chapter IDs."""
    for f in _all_temp_audio_files():
        try:
            # Parse chapter id from "chapter_123.wav" or "chapter_123_seg_0.wav"
            parts = f.stem.split("_")
            ch_id = int(parts[1])
            if ch_id not in keep_ids:
                f.unlink()
                logger.debug("Deleted temp file: %s", f.name)
        except (ValueError, IndexError):
            pass
    # Clean streaming state for removed chapters
    for ch_id in list(_streaming_state.keys()):
        if ch_id not in keep_ids:
            _streaming_state.pop(ch_id, None)


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

    logger.info("Synthesizing chapter %d to file (voice=%s, speed=%.1f)", chapter_id, voice, speed)
    start = time.time()

    pipeline = await get_pipeline(voice)
    loop = asyncio.get_event_loop()
    segments = await loop.run_in_executor(
        _executor,
        _synthesize_text_blocking, pipeline, text, voice, speed
    )

    wav_bytes = _segments_to_wav_bytes(segments)
    output_path.write_bytes(wav_bytes)

    elapsed = time.time() - start
    duration = sum(len(s) for s in segments) / SAMPLE_RATE
    logger.info(
        "Chapter %d synthesized: %.1fs audio in %.1fs (%.1fx realtime)",
        chapter_id, duration, elapsed, duration / elapsed if elapsed > 0 else 0
    )

    return output_path



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


# Inter-segment silence baked into the concatenated full file (see
# _segments_to_wav_bytes). AAC segments get the same amount of trailing pad so
# the HLS timeline and the full-file timeline line up for progress save/resume.
SEGMENT_GAP_SECONDS = 0.3


def _encode_segment_aac(chapter_id: int, index: int) -> bool:
    """
    Encode a WAV segment to packed ADTS AAC for native HLS playback (iOS).
    Returns False (and logs) if ffmpeg is unavailable or fails — the WAV
    segment fallback still works in that case.
    """
    wav = _segment_path(chapter_id, index)
    aac = _aac_segment_path(chapter_id, index)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(wav),
        "-af", f"apad=pad_dur={SEGMENT_GAP_SECONDS}",
        "-c:a", "aac", "-b:a", "96k",
        str(aac),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60,
                       creationflags=creationflags)
        return True
    except Exception as e:
        logger.warning("AAC encode failed for chapter %d seg %d: %s", chapter_id, index, e)
        return False


def _save_segment_wav(chapter_id: int, index: int, audio: np.ndarray) -> float:
    """Save a single segment as a WAV file. Returns duration in seconds."""
    path = _segment_path(chapter_id, index)
    buf = io.BytesIO()
    sf.write(buf, audio, SAMPLE_RATE, format="WAV")
    path.write_bytes(buf.getvalue())
    return len(audio) / SAMPLE_RATE


async def synthesize_chapter_streaming(
    chapter_id: int,
    text: str,
    voice: str = "af_heart",
    speed: float = 1.0,
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

    pipeline = await get_pipeline(voice)
    loop = asyncio.get_event_loop()
    all_segments: list[np.ndarray] = []

    def _produce_segments():
        seg_index = 0
        try:
            generator = pipeline(text, voice=voice, speed=speed, split_pattern=r'\n+')
            for graphemes, phonemes, audio in generator:
                if audio is not None and len(audio) > 0:
                    all_segments.append(audio)
                    dur = _save_segment_wav(chapter_id, seg_index, audio)
                    _encode_segment_aac(chapter_id, seg_index)
                    st = _streaming_state.get(chapter_id)
                    if st is not None:
                        st["segments"].append(dur)
                        st["total_duration"] += dur
                    seg_index += 1
        except Exception as e:
            logger.error("Streaming synthesis error for chapter %d: %s", chapter_id, e)

    await loop.run_in_executor(_executor, _produce_segments)

    # Save complete concatenated file
    if all_segments:
        wav_bytes = _segments_to_wav_bytes(all_segments)
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


# Prefetch state
_prefetch_task: Optional[asyncio.Task] = None


async def prefetch_next_chapter(
    chapter_id: int,
    text: str,
    voice: str = "af_heart",
    speed: float = 1.0,
):
    """Start prefetching the next chapter in the background."""
    global _prefetch_task

    # Cancel any existing prefetch
    if _prefetch_task and not _prefetch_task.done():
        _prefetch_task.cancel()

    async def _do_prefetch():
        try:
            await synthesize_chapter_to_file(chapter_id, text, voice, speed)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Prefetch error for chapter %d: %s", chapter_id, e)

    _prefetch_task = asyncio.create_task(_do_prefetch())
