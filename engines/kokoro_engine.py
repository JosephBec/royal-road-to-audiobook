"""
Kokoro-82M engine.

Fast (~50x realtime on a 2070) with a fixed set of baked-in voices, listed in
config.yaml. This is the original engine the app shipped with and stays the
default: it is the only one cheap enough for bulk M4B exports.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, TYPE_CHECKING

import yaml

from engines.base import TTSEngine, Voice

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
FALLBACK_VOICES = [Voice("af_heart", "Heart (Female, American)")]


class KokoroEngine(TTSEngine):
    name = "kokoro"
    label = "Kokoro-82M (fast)"
    sample_rate = 24000
    supports_speed = True

    def __init__(self):
        self._pipeline = None

    def available(self) -> tuple[bool, str]:
        try:
            import kokoro  # noqa: F401
        except ImportError:
            return False, "kokoro is not installed"
        return True, ""

    def voices(self) -> list[Voice]:
        if not CONFIG_PATH.exists():
            return list(FALLBACK_VOICES)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            logger.exception("Could not read %s", CONFIG_PATH)
            return list(FALLBACK_VOICES)
        voices = [
            Voice(v["id"], v.get("label", v["id"]))
            for v in config.get("voices", []) if v.get("id")
        ]
        return voices or list(FALLBACK_VOICES)

    def default_voice(self) -> str:
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    return (yaml.safe_load(f) or {}).get("default_voice", "af_heart")
            except Exception:
                pass
        return "af_heart"

    def load(self) -> None:
        if self._pipeline is not None:
            return
        import torch
        from kokoro import KPipeline

        device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cpu":
            logger.warning("CUDA not available, Kokoro falling back to CPU.")
        else:
            props = torch.cuda.get_device_properties(0)
            logger.info("Kokoro on CUDA: %s (%.1f GB VRAM)",
                        torch.cuda.get_device_name(0), props.total_memory / (1024 ** 3))
        self._pipeline = KPipeline(lang_code="a", device=device)
        logger.info("Kokoro pipeline initialized.")

    def synthesize(self, text: str, voice: str, speed: float) -> Iterator["np.ndarray"]:
        self.load()
        # Kokoro splits on blank lines itself and yields one array per chunk,
        # which maps straight onto the segment streaming in tts.py.
        for _graphemes, _phonemes, audio in self._pipeline(
            text, voice=voice, speed=speed, split_pattern=r"\n+"
        ):
            if audio is not None and len(audio) > 0:
                yield audio

    def unload(self) -> None:
        if self._pipeline is None:
            return
        self._pipeline = None
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass
        logger.info("Kokoro pipeline unloaded.")
