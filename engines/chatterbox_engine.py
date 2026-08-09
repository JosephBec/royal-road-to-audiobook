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

import json
import logging
import re
from pathlib import Path
from typing import Iterator, TYPE_CHECKING

from engines.base import (
    TTSEngine, Voice, VOICES_DIR, MIN_SEGMENT_GAP, MAX_SEGMENT_GAP,
)

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

BUILTIN_VOICE_ID = "cb_builtin"
# Hard ceiling per chunk. Turbo's tokenizer truncates past its window, so no
# chunk may exceed this — but chunks are normally one sentence each, well under.
MAX_CHUNK_CHARS = 300

# Chatterbox runs sentences together. It emits ~0.47s of its own silence at a
# period, so the inherited 0.3s gap was inaudible; 0.7s is the first value that
# reads as a deliberate beat rather than a stumble.
DEFAULT_SENTENCE_PAUSE = 0.7
SETTINGS_FILENAME = "voice_settings.json"
# The model asserts on reference audio of 5s or less.
MIN_REFERENCE_SECONDS = 5.0
REFERENCE_SUFFIXES = (".wav", ".mp3", ".flac", ".ogg", ".m4a")


def _split_chunks(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split text into one chunk per sentence.

    One sentence per chunk is what makes the inter-segment pause land on every
    period. Packing several sentences into a 300-char chunk (the previous
    behaviour) left the model to pace the interior itself, and it rushes — the
    result reads as one long run-on sentence.

    An over-long sentence is still broken on commas rather than truncated,
    because Turbo's tokenizer silently drops anything past its window.
    """
    chunks: list[str] = []
    for para in (p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()):
        for sentence in re.split(r"(?<=[.!?\"'”])\s+", para):
            sentence = sentence.strip()
            while len(sentence) > max_chars:
                cut = max(sentence.rfind(", ", 0, max_chars),
                          sentence.rfind(" ", 0, max_chars))
                if cut <= 0:
                    cut = max_chars
                head, sentence = sentence[:cut].strip(), sentence[cut:].strip()
                if head:
                    chunks.append(head)
            if sentence:
                chunks.append(sentence)
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

    # ----- per-voice tuning -----
    #
    # Stored in one JSON file beside the clips rather than a DB table: the
    # voices/ folder is already the source of truth for what exists, so the
    # tuning lives with it and survives a database reset.

    def _settings_path(self) -> Path:
        return VOICES_DIR / SETTINGS_FILENAME

    def _load_settings(self) -> dict:
        path = self._settings_path()
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            logger.exception("Could not read %s — using defaults", path)
            return {}

    def segment_gap(self, voice_id: str) -> float:
        raw = self._load_settings().get(voice_id, {}).get("sentence_pause")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return DEFAULT_SENTENCE_PAUSE
        return max(MIN_SEGMENT_GAP, min(MAX_SEGMENT_GAP, value))

    def voice_settings(self, voice_id: str) -> dict:
        return {
            "sentence_pause": self.segment_gap(voice_id),
            "sentence_pause_default": DEFAULT_SENTENCE_PAUSE,
            "min": MIN_SEGMENT_GAP,
            "max": MAX_SEGMENT_GAP,
        }

    def set_voice_settings(self, voice_id: str, **values) -> dict:
        if "sentence_pause" in values:
            pause = float(values["sentence_pause"])
            if not (MIN_SEGMENT_GAP <= pause <= MAX_SEGMENT_GAP):
                raise ValueError(
                    f"sentence_pause must be between {MIN_SEGMENT_GAP} and {MAX_SEGMENT_GAP}")
            settings = self._load_settings()
            settings.setdefault(voice_id, {})["sentence_pause"] = round(pause, 2)
            VOICES_DIR.mkdir(exist_ok=True)
            tmp = self._settings_path().with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, sort_keys=True)
            tmp.replace(self._settings_path())  # atomic: never a half-written file
            logger.info("Voice %s sentence_pause -> %.2fs", voice_id, pause)
        return self.voice_settings(voice_id)

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
