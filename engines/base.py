"""
TTS engine interface.

An engine owns exactly one thing: turning text into audio segments. Everything
around that — temp files, caching, retention, segment streaming, HLS/AAC
encoding — lives in tts.py and is engine-agnostic.

Heavy ML imports (torch, kokoro, chatterbox) must stay inside `load()` and
`synthesize()`, never at module scope, so the registry can enumerate engines on
a machine that has none of them installed (this is what lets CI import the app).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


# Reference clips for cloning engines. Mirrors the EPUBs folder convention:
# drop a file in, it becomes selectable. Override for tests.
VOICES_DIR = Path(os.environ.get(
    "NOVEL_TTS_VOICES_DIR",
    str(Path(__file__).resolve().parent.parent / "voices"),
))


@dataclass(frozen=True)
class Voice:
    """A selectable voice. `id` is what gets stored in settings."""
    id: str
    label: str
    # True when the voice comes from a user-supplied reference clip rather than
    # being baked into the model. Cached audio must be invalidated when the
    # clip's mtime changes, which a voice id alone can't express.
    custom: bool = False
    source_path: Path | None = None

    def fingerprint(self) -> str:
        """Identity for cache invalidation: id plus clip mtime for custom voices."""
        if self.custom and self.source_path is not None:
            try:
                return f"{self.id}:{self.source_path.stat().st_mtime_ns}"
            except OSError:
                pass
        return self.id


class TTSEngine:
    """Base class for synthesis backends. Subclasses live in this package and
    are auto-discovered — see engines/__init__.py."""

    name: str = ""            # stable id stored in settings.engine
    label: str = ""           # shown in the UI
    sample_rate: int = 24000

    # Kokoro varies its own synthesis rate; Chatterbox has no speed parameter,
    # so exports have to time-stretch the rendered audio instead. Playback speed
    # is always client-side (audio.playbackRate) regardless.
    supports_speed: bool = True

    def available(self) -> tuple[bool, str]:
        """(usable, reason). False keeps the engine visible but unselectable so
        a missing dependency shows up in the UI instead of failing at play time."""
        return True, ""

    def voices(self) -> list[Voice]:
        raise NotImplementedError

    def default_voice(self) -> str:
        voices = self.voices()
        return voices[0].id if voices else ""

    def resolve_voice(self, voice_id: str) -> Voice | None:
        return next((v for v in self.voices() if v.id == voice_id), None)

    def load(self) -> None:
        """Load model weights. Blocking; called once on the TTS worker thread."""
        raise NotImplementedError

    def synthesize(self, text: str, voice: str, speed: float) -> Iterator["np.ndarray"]:
        """Yield float32 mono audio segments at self.sample_rate.

        Blocking generator, run on the TTS worker thread. Yielding progressively
        (rather than returning one array) is what makes Instant Play work: each
        yielded segment is written out and becomes playable immediately.
        """
        raise NotImplementedError

    def unload(self) -> None:
        """Release VRAM. Called when switching engines."""
        pass
