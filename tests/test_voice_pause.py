"""Per-voice sentence pause: chunking, persistence, and timeline alignment.

The pause is baked into rendered audio in three places — the concatenated WAV,
the AAC segment padding and the HLS playlist's duration maths. If they ever
disagree, a saved playback position resumes in the wrong spot.
"""
import numpy as np
import pytest

import engines
import tts
from engines.base import VOICES_DIR, MAX_SEGMENT_GAP
from engines.chatterbox_engine import (
    DEFAULT_SENTENCE_PAUSE, MAX_CHUNK_CHARS, _split_chunks,
)


@pytest.fixture()
def voices_dir(tmp_path, monkeypatch):
    """Isolated voices/ so tests never touch the real settings file."""
    import engines.base as base
    import engines.chatterbox_engine as cb
    d = tmp_path / "voices"
    d.mkdir()
    monkeypatch.setattr(base, "VOICES_DIR", d)
    monkeypatch.setattr(cb, "VOICES_DIR", d)
    return d


# ----- chunking: one sentence per chunk is what puts a pause at every period -----

def test_each_sentence_becomes_its_own_chunk():
    text = "First one here. Second one here. Third one here."
    assert _split_chunks(text) == [
        "First one here.", "Second one here.", "Third one here."]


def test_short_sentences_are_not_merged():
    """Merging would swallow the pause between them."""
    assert len(_split_chunks("Yes. No. Maybe.")) == 3


def test_overlong_sentence_still_split_under_the_window():
    text = "word " * 200  # no sentence break, well past the tokenizer window
    chunks = _split_chunks(text)
    assert all(len(c) <= MAX_CHUNK_CHARS for c in chunks)
    assert len(" ".join(chunks).split()) == 200


def test_paragraphs_stay_separate():
    assert len(_split_chunks("One here.\n\nTwo here.")) == 2


# ----- per-voice settings -----

def test_default_pause_is_seven_tenths(voices_dir):
    cb = engines.get_engine("chatterbox")
    assert cb.segment_gap("cb_builtin") == pytest.approx(DEFAULT_SENTENCE_PAUSE)


def test_kokoro_keeps_the_original_gap():
    """Changing Chatterbox must not alter how Kokoro audio is assembled."""
    assert engines.get_engine("kokoro").segment_gap("af_heart") == pytest.approx(0.3)


def test_pause_persists_and_reloads(voices_dir):
    cb = engines.get_engine("chatterbox")
    cb.set_voice_settings("cb_builtin", sentence_pause=1.1)
    assert cb.segment_gap("cb_builtin") == pytest.approx(1.1)
    assert (voices_dir / "voice_settings.json").exists()


def test_pause_is_per_voice_not_global(voices_dir):
    cb = engines.get_engine("chatterbox")
    cb.set_voice_settings("cb_builtin", sentence_pause=1.2)
    assert cb.segment_gap("cb_other") == pytest.approx(DEFAULT_SENTENCE_PAUSE)


@pytest.mark.parametrize("bad", [-0.5, MAX_SEGMENT_GAP + 1])
def test_out_of_range_pause_is_rejected(voices_dir, bad):
    cb = engines.get_engine("chatterbox")
    with pytest.raises(ValueError):
        cb.set_voice_settings("cb_builtin", sentence_pause=bad)


def test_corrupt_settings_file_falls_back_to_default(voices_dir):
    (voices_dir / "voice_settings.json").write_text("{not json", encoding="utf-8")
    cb = engines.get_engine("chatterbox")
    assert cb.segment_gap("cb_builtin") == pytest.approx(DEFAULT_SENTENCE_PAUSE)


def test_kokoro_reports_no_tunable_settings():
    assert engines.get_engine("kokoro").supports_voice_settings() is False
    assert engines.get_engine("chatterbox").supports_voice_settings() is True


# ----- the gap actually lands in the audio -----

def test_gap_length_shows_up_between_segments():
    segs = [np.ones(2400, dtype=np.float32), np.ones(2400, dtype=np.float32)]
    short = tts._segments_to_wav_bytes(segs, 24000, 0.3)
    long = tts._segments_to_wav_bytes(segs, 24000, 0.7)
    assert len(long) > len(short)

    import io, soundfile as sf
    d_short = sf.info(io.BytesIO(short)).duration
    d_long = sf.info(io.BytesIO(long)).duration
    assert d_long - d_short == pytest.approx(0.4, abs=0.01)


def test_no_trailing_gap_after_the_last_segment():
    import io, soundfile as sf
    segs = [np.ones(24000, dtype=np.float32)]
    info = sf.info(io.BytesIO(tts._segments_to_wav_bytes(segs, 24000, 0.7)))
    assert info.duration == pytest.approx(1.0, abs=0.01)


def test_segment_gap_for_routes_to_the_engine(voices_dir):
    engines.get_engine("chatterbox").set_voice_settings("cb_builtin", sentence_pause=0.95)
    assert tts.segment_gap_for("chatterbox", "cb_builtin") == pytest.approx(0.95)
    assert tts.segment_gap_for("kokoro", "af_heart") == pytest.approx(0.3)


# ----- per-segment gaps -----

def test_gaps_may_differ_per_segment():
    """An ellipsis holds a longer beat than a full stop in the same chapter."""
    import io, soundfile as sf
    segs = [np.ones(2400, dtype=np.float32) for _ in range(3)]
    uniform = tts._segments_to_wav_bytes(segs, 24000, 0.7)
    varied = tts._segments_to_wav_bytes(segs, 24000, [2.1, 0.7, 0.7])
    d_uniform = sf.info(io.BytesIO(uniform)).duration
    d_varied = sf.info(io.BytesIO(varied)).duration
    # First gap widened 0.7 -> 2.1
    assert d_varied - d_uniform == pytest.approx(1.4, abs=0.01)


def test_a_single_gap_value_still_works():
    import io, soundfile as sf
    segs = [np.ones(24000, dtype=np.float32) for _ in range(2)]
    info = sf.info(io.BytesIO(tts._segments_to_wav_bytes(segs, 24000, 0.5)))
    assert info.duration == pytest.approx(2.5, abs=0.01)
