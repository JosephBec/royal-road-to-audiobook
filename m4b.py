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
