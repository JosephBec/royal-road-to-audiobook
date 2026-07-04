"""M4B audiobook assembly: naming, chapter metadata, and ffmpeg encoding.

Adapted from Ebook-to-Audiobook's audiobook_builder.py, reworked to read
chapter audio from WAV files on disk instead of holding arrays in RAM.
"""

import logging
import re
import shutil
import subprocess
from pathlib import Path

import soundfile as sf

logger = logging.getLogger(__name__)

_INVALID = set('<>:"/\\|?*')


def sanitize_title(title: str) -> str:
    """Make a string safe as a Windows filename component."""
    cleaned = "".join(" " if (c in _INVALID or ord(c) < 32) else c for c in title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(" .")
    return cleaned or "Untitled"


def export_basename(title: str, start: int, end: int) -> str:
    """Fixed naming format: 'Title - Chapters X - Y' (no author, not configurable)."""
    return f"{sanitize_title(title)} - Chapters {start} - {end}"


def _ffescape(s: str) -> str:
    """Escape ffmetadata special characters (=, ;, #, \\, newline)."""
    return re.sub(r"([=;#\\\n])", r"\\\1", s)


def ffmetadata_content(chapters: list) -> str:
    """Build an FFMETADATA1 file body from (title, duration_seconds) tuples."""
    lines = [";FFMETADATA1"]
    t_ms = 0
    for title, duration in chapters:
        end_ms = t_ms + int(duration * 1000)
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={t_ms}",
            f"END={end_ms}",
            f"title={_ffescape(title)}",
        ]
        t_ms = end_ms
    return "\n".join(lines)


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise RuntimeError("ffmpeg not found on PATH — required for M4B export.")
    return path


def _run(cmd: list, timeout: int, what: str):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{what} timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise RuntimeError(f"{what} failed: {result.stderr[-2000:]}")


def assemble_m4b(
    chapter_wavs: list,
    out_path: Path,
    *,
    book_title: str,
    author: str,
    cover_bytes: bytes | None = None,
    cover_ext: str = "jpg",
    bitrate: str = "64k",
) -> Path:
    """Concatenate chapter WAVs and encode a chaptered M4B. Blocking."""
    ffmpeg = _ffmpeg()
    out_path = Path(out_path)
    workdir = out_path.parent

    concat_list = workdir / "concat_list.txt"
    metadata_path = workdir / "metadata.txt"
    combined = workdir / "combined.wav"
    cover_path = None
    try:
        durations = [(title, sf.info(str(wav)).duration) for title, wav in chapter_wavs]
        total = sum(d for _, d in durations)

        concat_list.write_text(
            "".join(f"file '{str(wav).replace(chr(39), chr(39) + chr(92) + chr(39) * 2)}'\n"
                    for _, wav in chapter_wavs),
            encoding="utf-8")

        metadata_path.write_text(ffmetadata_content(durations), encoding="utf-8")

        _run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
              "-c", "copy", str(combined)],
             timeout=max(600, int(total * 0.5) + 600), what="WAV concat")

        if cover_bytes:
            cover_path = workdir / f"cover.{cover_ext}"
            cover_path.write_bytes(cover_bytes)

        cmd = [ffmpeg, "-y", "-i", str(combined), "-i", str(metadata_path)]
        if cover_path:
            cmd += ["-i", str(cover_path),
                    "-map", "0:a", "-map", "2:v", "-map_metadata", "1",
                    "-c:a", "aac", "-b:a", bitrate, "-ar", "24000", "-ac", "1",
                    "-c:v", "copy", "-disposition:v", "attached_pic"]
        else:
            cmd += ["-map_metadata", "1",
                    "-c:a", "aac", "-b:a", bitrate, "-ar", "24000", "-ac", "1"]
        cmd += ["-metadata", f"title={book_title}",
                "-metadata", f"artist={author}",
                "-metadata", f"album={book_title}",
                "-metadata", "genre=Audiobook",
                "-f", "mp4", str(out_path)]
        _run(cmd, timeout=max(1200, int(total) + 1200), what="M4B encode")
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise
    finally:
        combined.unlink(missing_ok=True)
        concat_list.unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)
        if cover_path:
            cover_path.unlink(missing_ok=True)

    logger.info("M4B assembled: %s (%.1f min)", out_path, total / 60)
    return out_path
