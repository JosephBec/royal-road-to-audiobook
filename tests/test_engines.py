"""Engine registry, voice resolution and Chatterbox chunking.

No model weights are loaded here — everything under test is the plumbing that
decides *which* engine and voice get used, which is where the bugs live.
"""
import pytest

import engines
from engines.base import TTSEngine, Voice
from engines.chatterbox_engine import _split_chunks, MAX_CHUNK_CHARS


def test_both_engines_are_discovered():
    found = engines.discover_engines()
    assert "kokoro" in found
    assert "chatterbox" in found


def test_default_engine_is_kokoro():
    # Kokoro stays the default: it is the only engine fast enough for exports.
    assert engines.DEFAULT_ENGINE == "kokoro"
    assert engines.get_engine(None).name == "kokoro"


def test_unknown_engine_falls_back_to_default():
    assert engines.get_engine("does-not-exist").name == "kokoro"


def test_kokoro_exposes_config_voices():
    kokoro = engines.get_engine("kokoro")
    ids = {v.id for v in kokoro.voices()}
    assert "af_heart" in ids
    assert kokoro.default_voice() in ids
    assert kokoro.supports_speed is True


def test_chatterbox_has_builtin_voice_and_no_speed():
    cb = engines.get_engine("chatterbox")
    assert cb.default_voice() == "cb_builtin"
    # No speed parameter — exports time-stretch instead (see export_worker).
    assert cb.supports_speed is False


@pytest.mark.parametrize("engine_name,stale_voice", [
    ("kokoro", "cb_builtin"),      # a Chatterbox voice while on Kokoro
    ("chatterbox", "af_heart"),    # a Kokoro voice while on Chatterbox
])
def test_resolve_voice_rejects_other_engines_voices(engine_name, stale_voice):
    """Switching engines leaves a voice id the new engine has never heard of."""
    resolved = engines.resolve_voice(engine_name, stale_voice)
    engine = engines.get_engine(engine_name)
    assert resolved == engine.default_voice()
    assert engine.resolve_voice(resolved) is not None


def test_resolve_voice_keeps_a_valid_voice():
    assert engines.resolve_voice("kokoro", "am_michael") == "am_michael"


def test_custom_voice_fingerprint_tracks_mtime(tmp_path):
    clip = tmp_path / "narrator.wav"
    clip.write_bytes(b"x")
    voice = Voice("cb_narrator", "narrator", custom=True, source_path=clip)
    first = voice.fingerprint()

    import os
    os.utime(clip, (0, 0))  # simulate the clip being replaced
    assert voice.fingerprint() != first, "edited clip must invalidate cached audio"


def test_builtin_voice_fingerprint_is_just_the_id():
    assert Voice("cb_builtin", "Built-in").fingerprint() == "cb_builtin"


# ----- Chatterbox chunking: truncation here means silently losing text -----

def test_chunks_stay_within_the_model_window():
    text = " ".join(f"Sentence number {i} carries on for a while." for i in range(60))
    chunks = _split_chunks(text)
    assert chunks
    assert all(len(c) <= MAX_CHUNK_CHARS for c in chunks), \
        "a chunk over the window would be silently truncated by the tokenizer"


def test_chunking_preserves_all_words():
    text = ("First paragraph with several words in it.\n\n"
            "Second paragraph, also of a reasonable length, continues here.")
    assert " ".join(_split_chunks(text)).split() == text.split()


def test_a_single_overlong_sentence_is_split_not_dropped():
    text = "word " * 200  # one 1000-char run with no sentence break
    chunks = _split_chunks(text)
    assert len(chunks) > 1
    assert all(len(c) <= MAX_CHUNK_CHARS for c in chunks)
    assert len(" ".join(chunks).split()) == 200


def test_paragraphs_are_not_merged_into_one_chunk():
    text = "Short one.\n\nShort two."
    assert len(_split_chunks(text)) == 2


def test_empty_text_yields_no_chunks():
    assert _split_chunks("   \n\n  ") == []


def test_base_engine_requires_implementation():
    class Incomplete(TTSEngine):
        name = "incomplete"

    with pytest.raises(NotImplementedError):
        Incomplete().voices()
