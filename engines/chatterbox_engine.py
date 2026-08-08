"""
Chatterbox-Turbo engine (Resemble AI, MIT).

Higher quality than Kokoro and ~29x slower — measured at RTF 0.47 (2.1x
realtime) on an RTX 2070, so it still renders faster than playback. Voices are
zero-shot clones from reference clips: one is baked into the model, the rest
come from WAV files dropped in the voices/ folder.

Two behaviours worth knowing before editing this file:

* Turbo's tokenizer has truncation=True, so text past its window is silently
  dropped — no error, just short audio. Everything must be chunked.
* There is no speed parameter. Playback speed is client-side (audio.playbackRate)
  so that doesn't matter for listening, but exports have to time-stretch instead.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterator, TYPE_CHECKING

from engines.base import TTSEngine, Voice, VOICES_DIR

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

BUILTIN_VOICE_ID = "cb_builtin"
# Turbo degrades and eventually truncates on long input; ~300 chars keeps every
# chunk well inside the window while staying long enough for natural prosody.
MAX_CHUNK_CHARS = 300
# The model asserts on reference audio of 5s or less.
MIN_REFERENCE_SECONDS = 5.0
REFERENCE_SUFFIXES = (".wav", ".mp3", ".flac", ".ogg", ".m4a")


def _split_chunks(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split text into synthesis chunks on paragraph, then sentence, boundaries.

    Paragraphs are kept apart so the model doesn't run them together, and an
    over-long sentence is broken on commas rather than truncated.
    """
    chunks: list[str] = []
    for para in (p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()):
        sentences = re.split(r"(?<=[.!?\"'”])\s+", para)
        current = ""
        for sentence in sentences:
            while len(sentence) > max_chars:
                # No sentence break available — cut at the last comma/space that fits.
                cut = max(sentence.rfind(", ", 0, max_chars),
                          sentence.rfind(" ", 0, max_chars))
                if cut <= 0:
                    cut = max_chars
                head, sentence = sentence[:cut].strip(), sentence[cut:].strip()
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(head)
            if not sentence:
                continue
            if current and len(current) + len(sentence) + 1 > max_chars:
                chunks.append(current)
                current = sentence
            else:
                current = f"{current} {sentence}".strip()
        if current:
            chunks.append(current)
    return chunks


class ChatterboxEngine(TTSEngine):
    name = "chatterbox"
    label = "Chatterbox-Turbo (high quality)"
    sample_rate = 24000
    supports_speed = False

    def __init__(self):
        self._model = None
        self._builtin_conds = None
        self._conds_cache: dict[str, object] = {}
        self._active_voice: str | None = None

    def available(self) -> tuple[bool, str]:
        try:
            import chatterbox  # noqa: F401
        except ImportError:
            return False, "chatterbox-tts is not installed"
        return True, ""

    # ----- voices -----

    def voices(self) -> list[Voice]:
        voices = [Voice(BUILTIN_VOICE_ID, "Chatterbox (built-in)")]
        for path in sorted(self._reference_files()):
            voices.append(Voice(
                id=f"cb_{path.stem}",
                label=f"{path.stem} (custom)",
                custom=True,
                source_path=path,
            ))
        return voices

    def _reference_files(self) -> list[Path]:
        if not VOICES_DIR.exists():
            return []
        return [p for p in VOICES_DIR.iterdir()
                if p.is_file() and p.suffix.lower() in REFERENCE_SUFFIXES]

    def default_voice(self) -> str:
        return BUILTIN_VOICE_ID

    # ----- model -----

    def load(self) -> None:
        if self._model is not None:
            return
        import numpy as np
        import torch
        from chatterbox.tts_turbo import ChatterboxTurboTTS

        # numpy 2 (NEP 50) promotes float32_array * np.float64 to float64, which
        # sends the reference waveform into the mel/LSTM stack as double and
        # crashes. numpy 1.x — what this app pins on Python 3.12 — is unaffected,
        # so only patch when we're actually on numpy 2.
        numpy_major = int(np.__version__.split(".")[0])
        if numpy_major >= 2 and not getattr(ChatterboxTurboTTS, "_norm_patched", False):
            _orig = ChatterboxTurboTTS.norm_loudness

            def _norm_f32(self, wav, sr, target_lufs=-27):
                return np.asarray(_orig(self, wav, sr, target_lufs), dtype=np.float32)

            ChatterboxTurboTTS.norm_loudness = _norm_f32
            ChatterboxTurboTTS._norm_patched = True
            logger.info("Applied numpy 2 float32 patch to Chatterbox norm_loudness")

        device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cpu":
            logger.warning("CUDA not available — Chatterbox on CPU will be far slower than realtime.")
        self._model = ChatterboxTurboTTS.from_pretrained(device=device)
        self._builtin_conds = self._model.conds
        self._active_voice = BUILTIN_VOICE_ID
        logger.info("Chatterbox-Turbo initialized (device=%s, sr=%d)", device, self._model.sr)

    def _apply_voice(self, voice_id: str) -> None:
        """Point the model at the requested voice, preparing conditionals once.

        prepare_conditionals costs ~3s, so cache per voice and only swap when the
        selection actually changes.
        """
        if self._active_voice == voice_id:
            return
        if voice_id == BUILTIN_VOICE_ID:
            self._model.conds = self._builtin_conds
            self._active_voice = voice_id
            return

        voice = self.resolve_voice(voice_id)
        if voice is None or voice.source_path is None:
            logger.warning("Unknown Chatterbox voice %r — using built-in", voice_id)
            self._model.conds = self._builtin_conds
            self._active_voice = BUILTIN_VOICE_ID
            return

        key = voice.fingerprint()  # includes mtime, so an edited clip re-prepares
        conds = self._conds_cache.get(key)
        if conds is None:
            self._model.prepare_conditionals(str(voice.source_path))
            conds = self._model.conds
            self._conds_cache[key] = conds
            logger.info("Prepared Chatterbox conditionals for %s", voice.id)
        self._model.conds = conds
        self._active_voice = voice_id

    def synthesize(self, text: str, voice: str, speed: float) -> Iterator["np.ndarray"]:
        self.load()
        self._apply_voice(voice)

        chunks = _split_chunks(text)
        logger.debug("Chatterbox: %d chars -> %d chunk(s)", len(text), len(chunks))
        for chunk in chunks:
            wav = self._model.generate(chunk)
            audio = wav.squeeze(0).detach().cpu().numpy()
            if audio.size:
                yield audio

    def unload(self) -> None:
        if self._model is None:
            return
        self._model = None
        self._builtin_conds = None
        self._conds_cache.clear()
        self._active_voice = None
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass
        logger.info("Chatterbox-Turbo unloaded.")
