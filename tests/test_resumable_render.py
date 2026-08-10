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


def test_single_segment_is_reusable_once_recorded(temp_dir):
    """One finished segment is still a segment worth keeping."""
    fp = _fingerprint()
    _write_segments(4, 1)
    tts._aac_segment_path(4, 0).write_bytes(b"aac")
    tts.record_segment_duration(4, 0, 1.0, fp)
    assert tts.resumable_segment_count(4, fp) == 1


def test_a_finished_head_start_is_reused_whole(temp_dir):
    """Pressing play must not throw away the last segment and redo it.

    The duration is recorded only after the WAV and the AAC are both written,
    so a recorded segment is complete by construction. Discarding the newest
    one unconditionally meant every play cost a re-render of one chunk.
    """
    fp = _fingerprint()
    _write_segments(40, 6)
    for index in range(6):
        tts._aac_segment_path(40, index).write_bytes(b"aac")
        tts.record_segment_duration(40, index, 1.0, fp)
    assert tts.resumable_segment_count(40, fp) == 6


def test_an_unrecorded_last_segment_is_still_dropped(temp_dir):
    """The crash case: killed between writing the WAV and recording it."""
    fp = _fingerprint()
    _write_segments(41, 6)
    for index in range(5):          # the sixth never got a duration
        tts._aac_segment_path(41, index).write_bytes(b"aac")
        tts.record_segment_duration(41, index, 1.0, fp)
    assert tts.resumable_segment_count(41, fp) == 5


def test_a_recorded_segment_missing_its_aac_is_dropped(temp_dir):
    """The playlist serves AAC; a duration without one would advertise a 404."""
    fp = _fingerprint()
    _write_segments(42, 3)
    for index in range(3):
        tts.record_segment_duration(42, index, 1.0, fp)
    tts._aac_segment_path(42, 0).write_bytes(b"aac")
    tts._aac_segment_path(42, 1).write_bytes(b"aac")   # index 2 has no AAC
    assert tts.resumable_segment_count(42, fp) == 2


# ----- not rendering the same chapter twice -----

def test_is_rendering_is_false_for_an_untouched_chapter(temp_dir):
    assert tts.is_rendering(9999) is False


def test_is_rendering_tracks_the_chapter_lock(temp_dir):
    """Pressing play twice used to queue a second render behind the first,
    which then waited inside interactive_synthesis() while only blocked on a
    lock — so the prefetch worker kept yielding to it for nothing."""
    import asyncio

    async def check():
        lock = tts._synth_locks.setdefault(4242, asyncio.Lock())
        assert tts.is_rendering(4242) is False
        async with lock:
            assert tts.is_rendering(4242) is True
        assert tts.is_rendering(4242) is False

    asyncio.run(check())
    tts._synth_locks.pop(4242, None)


def test_precision_change_prevents_a_resume(temp_dir):
    """fp16 and fp32 do the same arithmetic to different numbers of digits.

    These models sample, so a tiny numeric difference can select a different
    token. Resuming an fp32 render in fp16 would splice two subtly different
    voices into one chapter — audible exactly once, halfway through.
    """
    fp32 = tts.render_fingerprint("body", "chatterbox", "cb_yearsley", 10, "fp32")
    fp16 = tts.render_fingerprint("body", "chatterbox", "cb_yearsley", 10, "fp16")
    assert fp32 != fp16

    _write_segments(30, 5)
    tts.record_segment_duration(30, 0, 1.0, fp32)
    assert tts.resumable_segment_count(30, fp32) == 4
    assert tts.resumable_segment_count(30, fp16) == 0


def test_precision_defaults_to_fp32(temp_dir):
    """Engines that never declare a precision keep their existing identity."""
    assert tts.render_fingerprint("b", "kokoro", "af_heart", 3)["precision"] == "fp32"


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


def test_discard_reaches_segments_behind_a_gap(temp_dir):
    """Segments on disk are not always a prefix.

    A retention sweep or an interrupted render can leave a hole, and a scan
    that stops at the first missing index never sees what is past it. Those
    survivors belong to a render that no longer applies: the next render only
    overwrites the indices it produces, so a shorter one would leave the old
    tail behind to be served as the end of the new chapter.
    """
    for index in (26, 27, 28):
        tts._segment_path(20, index).write_bytes(b"old")
        tts._aac_segment_path(20, index).write_bytes(b"old")

    tts.discard_segments_from(20, 0)

    assert not any(tts._segment_path(20, i).exists() for i in (26, 27, 28))
    assert not any(tts._aac_segment_path(20, i).exists() for i in (26, 27, 28))


def test_discard_keeps_segments_before_the_mark_across_a_gap(temp_dir):
    """Only the tail goes. A hole must not be read as the end of the run."""
    for index in (0, 1, 5, 9):
        tts._segment_path(21, index).write_bytes(b"x")

    tts.discard_segments_from(21, 5)

    assert [tts._segment_path(21, i).exists() for i in (0, 1, 5, 9)] == \
        [True, True, False, False]


def test_discard_trims_durations_for_removed_segments(temp_dir):
    """A duration left behind for a deleted segment makes the playlist
    advertise audio that is no longer on disk."""
    fp = _fingerprint()
    _write_segments(22, 4)
    for index in range(4):
        tts.record_segment_duration(22, index, 1.0, fp)

    tts.discard_segments_from(22, 2)

    assert tts._read_sidecar(22)["durations"] == [1.0, 1.0]


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


def test_concurrent_renders_do_not_destroy_each_other(fake_engine, temp_dir):
    """Playback and the head-start pass can target the same chapter.

    Each computes a resume point and discards the tail beyond it, so without a
    per-chapter lock the second caller deletes the first caller's segments —
    observed live as a chapter's progress collapsing from 106 segments to 12.
    """
    import asyncio

    async def both():
        await asyncio.gather(
            tts.synthesize_chapter_streaming(
                30, TEXT, "cb_yearsley", 1.0, "chatterbox", None, max_seconds=3),
            tts.synthesize_chapter_streaming(30, TEXT, "cb_yearsley", 1.0, "chatterbox"),
        )

    asyncio.run(both())

    # Serialized, so every sentence exists exactly once and the chapter finished.
    assert tts.temp_path_for_chapter(30).exists()
    assert all(tts._segment_path(30, i).exists() for i in range(6))
    assert not tts._segment_path(30, 6).exists()


def test_background_render_yields_mid_chapter(fake_engine, temp_dir, monkeypatch):
    """A head start must stand aside the moment the user presses play.

    Checking only before a chapter starts is not enough — one chapter is
    minutes of work on Chatterbox, so pressing play in the middle meant
    waiting it out.
    """
    import asyncio

    busy = {"value": True}
    monkeypatch.setattr(tts, "interactive_busy", lambda: busy["value"])

    async def scenario():
        task = asyncio.create_task(tts.synthesize_chapter_streaming(
            40, TEXT, "cb_yearsley", 1.0, "chatterbox",
            None, None, yield_to_interactive=True))
        await asyncio.sleep(0.2)
        rendered_while_busy = len(fake_engine.rendered)
        busy["value"] = False          # user's chapter finished
        await task
        return rendered_while_busy

    rendered_while_busy = asyncio.run(scenario())
    assert rendered_while_busy == 0, "should not have rendered while blocked"
    assert len(fake_engine.rendered) == 6, "and should finish once free"


def test_foreground_render_does_not_yield(fake_engine, temp_dir, monkeypatch):
    """The chapter the user is waiting on must never stand aside for itself."""
    import asyncio
    monkeypatch.setattr(tts, "interactive_busy", lambda: True)
    asyncio.run(tts.synthesize_chapter_streaming(41, TEXT, "cb_yearsley", 1.0, "chatterbox"))
    assert len(fake_engine.rendered) == 6


def test_head_start_covers_a_never_opened_novel(monkeypatch, temp_dir):
    """A novel with no saved progress is the one guaranteed to be cold.

    Requiring a progress row meant the first play of a brand-new book always
    paid the full model-load cost — exactly the case the head start exists for.
    """
    import asyncio
    import database
    import prefetch

    database.init_db()   # this module doesn't use the app fixture
    db = database.SessionLocal()
    novel = database.Novel(title="Never Opened", rr_url="https://example.com/fiction/7777/x")
    db.add(novel)
    db.flush()
    for order in (1, 2):
        db.add(database.Chapter(
            novel_id=novel.id, title=f"Chapter {order}", order=order,
            rr_url=f"https://example.com/fiction/7777/x/chapter/{order}",
            text="Some body text for the chapter."))
    db.commit()
    novel_id = novel.id
    first_id = (db.query(database.Chapter)
                .filter(database.Chapter.novel_id == novel_id)
                .order_by(database.Chapter.order).first().id)
    db.close()

    started = []

    async def fake_stream(chapter_id, text, voice, speed, engine_name,
                          chunk_voices=None, max_seconds=None, **kw):
        started.append((chapter_id, max_seconds))

    monkeypatch.setattr(prefetch.tts, "synthesize_chapter_streaming", fake_stream)
    monkeypatch.setattr(prefetch.tts, "temp_path_for_chapter",
                        lambda cid: temp_dir / f"absent_{cid}.wav")

    asyncio.run(prefetch.head_start_pass())

    assert first_id in [c for c, _ in started], \
        "the first chapter of an unopened novel should get a head start"
    assert all(m == prefetch.HEAD_START_SECONDS for _, m in started)

    db = database.SessionLocal()
    db.delete(db.query(database.Novel).filter(database.Novel.id == novel_id).first())
    db.commit()
    db.close()
