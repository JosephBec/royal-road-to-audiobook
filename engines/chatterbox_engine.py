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
import os
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
# Run the transformer's weights in fp16. Measured on an RTX 2070: 3.28 GB ->
# 2.42 GB, with an 8% speedup that is not the reason to do it — the VRAM is.
# Whisper transcribed fp16 and fp32 renders of the same four sentences to
# identical text, so this is not a quality trade.
#
# Set CHATTERBOX_FP32=1 to turn it off without touching code. The conversion
# also self-checks at load and falls back on its own, so this is an override
# rather than the safety net.
HALF_PRECISION = os.environ.get("CHATTERBOX_FP32", "").strip().lower() not in (
    "1", "true", "yes", "on")

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
    import voice_script

    chunks: list[str] = []
    # One splitter, shared with voice scripts: span index N must be chunk N or
    # per-character voices land on the wrong lines. It also normalises
    # ellipses and never emits a chunk with nothing speakable in it — a lone
    # full stop made the model hallucinate noise.
    for sentence in voice_script.split_sentences(text):
        while len(sentence) > max_chars:
            cut = max(sentence.rfind(", ", 0, max_chars),
                      sentence.rfind(" ", 0, max_chars))
            if cut <= 0:
                cut = max_chars
            head, sentence = sentence[:cut].strip(), sentence[cut:].strip()
            if head and voice_script.has_speakable_content(head):
                chunks.append(head)
        if sentence and voice_script.has_speakable_content(sentence):
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
        self._half = False

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

        if device == "cuda" and HALF_PRECISION:
            self._try_half_precision()

    # ----- fp16 -----

    def _cast_conds(self, conds) -> None:
        """Match the conditioning tensors to the transformer's dtype.

        The weights and their inputs have to agree; converting only the module
        fails at the first matmul with "mat1 and mat2 must have the same dtype".
        """
        import torch

        t3_cond = getattr(conds, "t3", None)
        if t3_cond is None:
            return
        for field in getattr(t3_cond, "__dataclass_fields__", {}):
            value = getattr(t3_cond, field, None)
            if torch.is_tensor(value) and value.dtype == torch.float32:
                setattr(t3_cond, field, value.half())

    def _half_output_is_sane(self) -> bool:
        """Render something real and check it is actually audio.

        fp16 tops out at 65504, and an activation past that becomes inf, then
        NaN, then silence or noise. That failure is invisible until you listen,
        so prove it here instead — on the model, not on a toy tensor.
        """
        import numpy as np

        wav = self._model.generate("The quick brown fox jumps over the lazy dog.")
        audio = wav.squeeze(0).detach().cpu().numpy()
        if audio.size == 0 or not np.isfinite(audio).all():
            return False
        rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        return rms > 1e-3      # not silence

    def _try_half_precision(self) -> None:
        """Run the transformer in fp16, or fall back to a clean fp32 model.

        Worth ~0.9 GB of VRAM on an 8 GB card, which is the point — the speed
        difference measured only 8%. t3 is the autoregressive stage and holds
        most of the parameters; s3gen is left alone because it runs once per
        chunk rather than once per token.

        A half-applied conversion is worse than none: the module would be fp16
        with fp32 inputs and every render would fail. So the recovery path
        throws the model away and reloads rather than trying to undo it.
        """
        import torch

        try:
            self._model.t3 = self._model.t3.half()
            self._cast_conds(self._builtin_conds)
            self._model.conds = self._builtin_conds
            if not self._half_output_is_sane():
                raise RuntimeError("fp16 produced unusable audio")
        except Exception:
            logger.exception("fp16 conversion failed — reloading Chatterbox in fp32")
            self._half = False
            self._reload_fp32()
            return

        self._half = True
        logger.info("Chatterbox transformer in fp16 (%.2f GB allocated)",
                    torch.cuda.memory_allocated() / 1e9)

    def _reload_fp32(self) -> None:
        import gc

        import torch
        from chatterbox.tts_turbo import ChatterboxTurboTTS

        self._model = None
        self._builtin_conds = None
        self._conds_cache.clear()
        gc.collect()
        torch.cuda.empty_cache()

        self._model = ChatterboxTurboTTS.from_pretrained(device="cuda")
        self._builtin_conds = self._model.conds
        self._active_voice = BUILTIN_VOICE_ID
        logger.info("Chatterbox-Turbo reloaded in fp32.")

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
            # Freshly prepared conditionals are fp32; the transformer may not be.
            if self._half:
                self._cast_conds(conds)
            self._conds_cache[key] = conds
            logger.info("Prepared Chatterbox conditionals for %s", voice.id)
        self._model.conds = conds
        self._active_voice = voice_id

    @property
    def precision(self) -> str:
        return "fp16" if self._half else "fp32"

    def plan_chunks(self, text: str) -> list[str]:
        # Each sentence is already an independent generate() call, so these are
        # natural yield points for the shared worker.
        return _split_chunks(text)

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
        self._half = False
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass
        logger.info("Chatterbox-Turbo unloaded.")
