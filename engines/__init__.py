"""
TTS engine registry.

Drop a new <engine>.py with a TTSEngine subclass in this directory and it is
auto-discovered — same convention as scrapers/. Broken modules are logged and
skipped so one bad engine can't take down the app.
"""

import importlib
import logging
import pkgutil

from engines.base import TTSEngine, Voice, VOICES_DIR

logger = logging.getLogger(__name__)

DEFAULT_ENGINE = "kokoro"

_engines: dict[str, TTSEngine] | None = None


def discover_engines() -> dict[str, TTSEngine]:
    """Import every module in this package and instantiate its engines."""
    global _engines
    if _engines is not None:
        return _engines
    _engines = {}
    pkg = importlib.import_module("engines")
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        if mod_info.name == "base":
            continue
        try:
            module = importlib.import_module(f"engines.{mod_info.name}")
        except Exception:
            logger.exception("Skipping broken engine module: %s", mod_info.name)
            continue
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, TTSEngine) and obj is not TTSEngine:
                inst = obj()
                _engines[inst.name] = inst
                logger.info("Registered TTS engine: %s", inst.name)
    return _engines


def get_engine(name: str | None) -> TTSEngine:
    """Look up an engine by name, falling back to the default."""
    engines = discover_engines()
    if name and name in engines:
        return engines[name]
    if name:
        logger.warning("Unknown TTS engine %r, falling back to %s", name, DEFAULT_ENGINE)
    return engines[DEFAULT_ENGINE]


def engine_names() -> list[str]:
    return list(discover_engines())


def resolve_voice(engine_name: str | None, voice_id: str | None) -> str:
    """Coerce a voice to one the engine actually has.

    Voices are engine-specific (Kokoro's baked-in names mean nothing to
    Chatterbox), so a stored voice can go stale the moment the engine changes.
    Resolving centrally means every caller — playback, prefetch, exports —
    degrades to the engine's default instead of erroring.
    """
    engine = get_engine(engine_name)
    if voice_id and engine.resolve_voice(voice_id) is not None:
        return voice_id
    return engine.default_voice()


def reset():
    """Clear the registry (tests only)."""
    global _engines
    _engines = None


__all__ = ["TTSEngine", "Voice", "VOICES_DIR", "DEFAULT_ENGINE",
           "discover_engines", "get_engine", "engine_names", "reset"]
