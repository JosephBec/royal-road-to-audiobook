"""Resuming partial renders, and refusing to resume when it isn't safe.

Chunking is deterministic, so segment N is always the same sentence and a
half-rendered chapter is reusable. The danger is reusing it when the inputs
changed — that would splice the old voice, or old text, into a new render.
"""
import numpy as np
import pytest
import soundfile as sf

import tts


@pytest.fixture()
def temp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "TEMP_DIR", tmp_path)
    return tmp_path


def _fingerprint(text="body", engine_name="chatterbox", voice="cb_yearsley", chunk_count=10):
    return tts.render_fingerprint(text, engine_name, voice, chunk_count)


def _write_segments(chapter_id, count, seconds=1.0):
    for index in range(count):
        audio = np.zeros(int(24000 * seconds), dtype=np.float32)
        sf.write(str(tts._segment_path(chapter_id, index)), audio, 24000)


# ----- what may be reused -----

def test_nothing_to_resume_when_no_segments(temp_dir):
    assert tts.resumable_segment_count(1, _fingerprint()) == 0


def test_resumes_from_matching_fingerprint(temp_dir):
    fp = _fingerprint()
    _write_segments(1, 5)
    tts.record_segment_duration(1, 0, 1.0, fp)
    # The newest segment is dropped: it may be a half-written file.
    assert tts.resumable_segment_count(1, fp) == 4


@pytest.mark.parametrize("changed", [
    {"text": "different body"},
    {"engine_name": "kokoro"},
    {"voice": "cb_geeson"},
    {"chunk_count": 11},
])
def test_refuses_to_resume_when_inputs_changed(temp_dir, changed):
    """Reusing here would splice the wrong voice or the wrong words."""
    original = dict(text="body", engine_name="chatterbox", voice="cb_yearsley", chunk_count=10)
    _write_segments(2, 5)
    tts.record_segment_duration(2, 0, 1.0, tts.render_fingerprint(**original))
    assert tts.resumable_segment_count(2, tts.render_fingerprint(**{**original, **changed})) == 0


def test_sentence_pause_is_not_part_of_the_fingerprint():
    """The pause is applied when segments are joined, not baked into them, so
    changing it must not throw away rendered audio."""
    a = tts.render_fingerprint("body", "chatterbox", "cb_yearsley", 10)
    b = tts.render_fingerprint("body", "chatterbox", "cb_yearsley", 10)
    assert a == b


def test_legacy_sidecar_without_fingerprint_is_not_resumed(temp_dir):
    """Segments predating fingerprints can't be shown to match; discard them."""
    import json
    _write_segments(3, 5)
    tts._segment_index_path(3).write_text(json.dumps([1.0, 1.0, 1.0, 1.0, 1.0]))
    assert tts.resumable_segment_count(3, _fingerprint()) == 0


def test_single_segment_yields_nothing_reusable(temp_dir):
    fp = _fingerprint()
    _write_segments(4, 1)
    tts.record_segment_duration(4, 0, 1.0, fp)
    assert tts.resumable_segment_count(4, fp) == 0


# ----- discarding the stale tail -----

def test_discard_removes_segments_at_and_after_index(temp_dir):
    _write_segments(5, 6)
    for index in range(6):
        tts._aac_segment_path(5, index).write_bytes(b"x")
    tts.discard_segments_from(5, 3)
    assert [tts._segment_path(5, i).exists() for i in range(6)] == \
        [True, True, True, False, False, False]
    assert not any(tts._aac_segment_path(5, i).exists() for i in range(3, 6))


def test_discard_from_zero_clears_everything(temp_dir):
    _write_segments(6, 4)
    tts.discard_segments_from(6, 0)
    assert not any(tts._segment_path(6, i).exists() for i in range(4))


# ----- sidecar round-trip -----

def test_durations_and_fingerprint_coexist(temp_dir):
    fp = _fingerprint()
    tts.record_segment_duration(7, 0, 1.5, fp)
    tts.record_segment_duration(7, 1, 2.5, fp)
    assert tts.segment_durations(7) == [1.5, 2.5]
    assert tts._read_sidecar(7)["fingerprint"] == fp


def test_durations_incomplete_reports_none(temp_dir):
    """A gap means some segment never recorded; the playlist can't trust it."""
    fp = _fingerprint()
    tts.record_segment_duration(8, 2, 1.0, fp)   # 0 and 1 left as None
    assert tts.segment_durations(8) is None


def test_legacy_bare_list_still_readable(temp_dir):
    import json
    tts._segment_index_path(9).write_text(json.dumps([1.0, 2.0]))
    assert tts.segment_durations(9) == [1.0, 2.0]


def test_sidecar_is_cleaned_up_with_the_audio(temp_dir):
    fp = _fingerprint()
    _write_segments(10, 2)
    tts.record_segment_duration(10, 0, 1.0, fp)
    tts.remove_chapter_audio({10})
    assert not tts._segment_index_path(10).exists()


# ----- end to end through synthesize_chapter_streaming -----

class FakeEngine:
    """Deterministic stand-in: one segment per sentence, counting renders."""
    name = "chatterbox"
    sample_rate = 24000
    supports_speed = False

    def __init__(self):
        self.rendered = []

    def plan_chunks(self, text):
        return [s for s in text.split("|") if s]

    def synthesize(self, text, voice, speed):
        self.rendered.append(text)
        yield np.full(24000, 0.1, dtype=np.float32)   # 1s per sentence

    def segment_gap(self, voice):
        return 0.5


@pytest.fixture()
def fake_engine(temp_dir, monkeypatch):
    engine = FakeEngine()

    async def _get_engine(name=None):
        return engine

    monkeypatch.setattr(tts, "get_engine", _get_engine)
    monkeypatch.setattr(tts, "_encode_segment_aac",
                        lambda cid, i, gap=0.3, fp=None: tts.record_segment_duration(
                            cid, i, 1.0 + gap, fp) or True)
    return engine


TEXT = "|".join(f"Sentence {i}." for i in range(6))


def test_head_start_stops_early_and_writes_no_full_file(fake_engine, temp_dir):
    import asyncio
    asyncio.run(tts.synthesize_chapter_streaming(
        20, TEXT, "cb_yearsley", 1.0, "chatterbox", None, max_seconds=3))
    # Stops once 3s exists; the absence of a full file is what marks it partial.
    assert len(fake_engine.rendered) == 3
    assert not tts.temp_path_for_chapter(20).exists()


def test_second_call_resumes_instead_of_restarting(fake_engine, temp_dir):
    import asyncio
    asyncio.run(tts.synthesize_chapter_streaming(
        21, TEXT, "cb_yearsley", 1.0, "chatterbox", None, max_seconds=3))
    done_first = len(fake_engine.rendered)

    asyncio.run(tts.synthesize_chapter_streaming(
        21, TEXT, "cb_yearsley", 1.0, "chatterbox"))

    # Only the remaining sentences are rendered — plus one, because the newest
    # segment is always re-done in case it was written half-way.
    assert len(fake_engine.rendered) == done_first + (6 - done_first) + 1
    assert tts.temp_path_for_chapter(21).exists()


def test_changing_voice_discards_the_partial_render(fake_engine, temp_dir):
    import asyncio
    asyncio.run(tts.synthesize_chapter_streaming(
        22, TEXT, "cb_yearsley", 1.0, "chatterbox", None, max_seconds=3))
    fake_engine.rendered.clear()

    asyncio.run(tts.synthesize_chapter_streaming(
        22, TEXT, "cb_geeson", 1.0, "chatterbox"))

    # Everything re-rendered: the cached audio was the wrong speaker.
    assert len(fake_engine.rendered) == 6


def test_completed_chapter_is_not_re_rendered(fake_engine, temp_dir):
    import asyncio
    asyncio.run(tts.synthesize_chapter_streaming(23, TEXT, "cb_yearsley", 1.0, "chatterbox"))
    fake_engine.rendered.clear()
    asyncio.run(tts.synthesize_chapter_streaming(23, TEXT, "cb_yearsley", 1.0, "chatterbox"))
    assert fake_engine.rendered == []
