"""Single render-ahead worker: dedups, skips rendered chapters, runs cleanup.

No GPU/network — synthesis and scraping are faked.
"""
import asyncio

import pytest


@pytest.fixture()
def pf_env(tmp_path, monkeypatch):
    import database
    import tts
    import prefetch
    database.init_db()  # _fetch_text opens a session; no chapter rows → scrape path
    monkeypatch.setattr(tts, "TEMP_DIR", tmp_path)

    synth_calls = []

    async def fake_synth(chapter_id, text, voice, speed, engine_name=None,
                         chunk_voices=None, max_seconds=None,
                         yield_to_interactive=False):
        synth_calls.append(chapter_id)
        # The real streaming path writes the full file once it finishes.
        tts.temp_path_for_chapter(chapter_id).write_bytes(b"wav")
    monkeypatch.setattr(prefetch.tts, "synthesize_chapter_streaming", fake_synth)

    async def unexpected(*a, **k):
        raise AssertionError(
            "render-ahead must use the resumable streaming path, not "
            "synthesize_chapter_to_file")
    monkeypatch.setattr(prefetch.tts, "synthesize_chapter_to_file", unexpected)

    class FakeScraper:
        async def scrape_chapter_text(self, url):
            return f"text for {url}"
    monkeypatch.setattr(prefetch, "get_scraper_for_url", lambda url: FakeScraper())

    async def no_wait():
        return
    monkeypatch.setattr(prefetch, "_wait_for_interactive_idle", no_wait)

    cleanup_calls = []
    monkeypatch.setattr(prefetch, "retention_policy", lambda db: (set(), set()))
    monkeypatch.setattr(prefetch.tts, "cleanup_temp_files",
                        lambda keep, expiring=None: cleanup_calls.append((keep, expiring)))

    prefetch.reset()
    return prefetch, tts, synth_calls, cleanup_calls, tmp_path


def _targets(*ids):
    return [(i, f"http://x/{i}", f"Chapter {i}") for i in ids]


def test_enqueue_dedups(pf_env):
    prefetch, tts, synth_calls, _, _ = pf_env
    prefetch.enqueue(_targets(1), "af_heart")
    prefetch.enqueue(_targets(1), "af_heart")  # same id again
    asyncio.run(prefetch.drain_once())
    assert synth_calls == [1]


def test_skips_already_rendered(pf_env):
    prefetch, tts, synth_calls, _, tmp_path = pf_env
    tts.temp_path_for_chapter(7).write_bytes(b"already")
    prefetch.enqueue(_targets(7), "af_heart")
    asyncio.run(prefetch.drain_once())
    assert synth_calls == []


def test_processes_targets_in_order(pf_env):
    prefetch, tts, synth_calls, _, _ = pf_env
    prefetch.enqueue(_targets(1, 2, 3), "af_heart")
    asyncio.run(prefetch.drain_once())
    assert synth_calls == [1, 2, 3]


def test_runs_retention_cleanup_when_drained(pf_env):
    prefetch, tts, synth_calls, cleanup_calls, _ = pf_env
    prefetch.enqueue(_targets(1), "af_heart")
    asyncio.run(prefetch.drain_once())
    assert synth_calls == [1]
    assert len(cleanup_calls) == 1


def test_no_cleanup_when_nothing_queued(pf_env):
    prefetch, tts, synth_calls, cleanup_calls, _ = pf_env
    asyncio.run(prefetch.drain_once())
    assert synth_calls == []
    assert cleanup_calls == []


def test_head_start_runs_before_render_ahead(pf_env, monkeypatch):
    """The opening of the chapter being listened to outranks the whole of the
    chapters that are not.

    Pressing play enqueues the next three chapters, and each is rendered in
    full — twenty minutes apiece at the throughput Chatterbox manages. With
    the head start at the end of the drain, the first two minutes of the
    chapter under the playhead sat behind an hour of work for chapters the
    listener had not reached, so it was still cold when they pressed play.
    """
    prefetch, tts, synth_calls, _, _ = pf_env
    order = []

    async def fake_head_start():
        order.append("head-start")
    monkeypatch.setattr(prefetch, "head_start_pass", fake_head_start)

    original = prefetch._process_one

    async def tracking_process(item):
        order.append(f"full-render:{item[0]}")
        await original(item)
    monkeypatch.setattr(prefetch, "_process_one", tracking_process)

    prefetch.enqueue(_targets(1, 2, 3), "af_heart")
    asyncio.run(prefetch.drain_once())

    assert order == ["head-start", "full-render:1", "full-render:2", "full-render:3"]


def test_render_ahead_uses_the_resumable_path(pf_env):
    """Render-ahead is the work most likely to be interrupted.

    It runs unattended for hours, and synthesize_chapter_to_file renders a
    whole chapter into memory before writing anything — twenty minutes of GPU
    that a restart discards entirely, leaving the next attempt to start from
    zero. The streaming path writes each segment as it goes and records a
    fingerprint, so an interruption costs one chunk instead of a chapter.
    """
    prefetch, tts, synth_calls, _, _ = pf_env
    resumable_args = {}

    async def capture(chapter_id, text, voice, speed, engine_name=None,
                      chunk_voices=None, max_seconds=None,
                      yield_to_interactive=False):
        synth_calls.append(chapter_id)
        resumable_args.update(max_seconds=max_seconds,
                              yield_to_interactive=yield_to_interactive)
        tts.temp_path_for_chapter(chapter_id).write_bytes(b"wav")
    prefetch.tts.synthesize_chapter_streaming = capture

    prefetch.enqueue(_targets(1), "af_heart")
    asyncio.run(prefetch.drain_once())

    assert synth_calls == [1]
    # No cap: render-ahead wants the whole chapter, unlike the head start.
    assert resumable_args["max_seconds"] is None
    # And it must step aside for anything the listener is waiting on.
    assert resumable_args["yield_to_interactive"] is True


def test_drain_once_does_nothing_when_nothing_is_queued(pf_env, monkeypatch):
    """drain_once is the queued-work path; the idle sweep is the worker loop's."""
    prefetch, _, _, _, _ = pf_env
    calls = []

    async def fake_head_start():
        calls.append(1)
    monkeypatch.setattr(prefetch, "head_start_pass", fake_head_start)

    asyncio.run(prefetch.drain_once())
    assert calls == []


def test_idle_worker_runs_the_head_start(pf_env, monkeypatch):
    """Nothing enqueues on a quiet server.

    Waiting only on the queue parked the worker indefinitely, so the head
    start never ran until something else happened to queue work — and idle
    time is exactly when it should be running, since its whole purpose is to
    have the opening ready before play is pressed.
    """
    prefetch, _, _, _, _ = pf_env
    calls = []

    async def fake_head_start():
        calls.append(1)
        if len(calls) >= 2:
            raise asyncio.CancelledError  # let the loop exit
    monkeypatch.setattr(prefetch, "head_start_pass", fake_head_start)
    monkeypatch.setattr(prefetch, "IDLE_TICK_SECONDS", 0.01)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await prefetch._worker_loop()
    asyncio.run(run())

    assert len(calls) == 2, "idle worker should sweep on every tick"


def test_head_start_skips_a_chapter_that_already_has_one(pf_env, monkeypatch):
    """The idle sweep must be free for chapters already done.

    Re-entering the render to discover there is nothing to do is not free:
    resuming deliberately drops the last segment as possibly truncated, so
    every tick would discard a segment and render it again.
    """
    prefetch, tts, _, _, _ = pf_env
    fp = {"text": "t", "engine": "chatterbox", "voice": "v", "chunks": 3}
    for index, seconds in enumerate((60.0, 60.0, 30.0)):
        tts.record_segment_duration(99, index, seconds, fp)

    assert prefetch._head_start_satisfied(99) is True
    assert prefetch._head_start_satisfied(1234) is False  # nothing on disk


def test_head_start_not_satisfied_by_a_partial_opening(pf_env):
    """Half a head start is not a head start; the sweep should finish it."""
    prefetch, tts, _, _, _ = pf_env
    fp = {"text": "t", "engine": "chatterbox", "voice": "v", "chunks": 2}
    tts.record_segment_duration(98, 0, 30.0, fp)

    assert prefetch._head_start_satisfied(98) is False


def test_is_busy_false_after_drain(pf_env):
    prefetch, tts, synth_calls, _, _ = pf_env
    prefetch.enqueue(_targets(1, 2), "af_heart")
    assert prefetch.is_busy() is True
    asyncio.run(prefetch.drain_once())
    assert prefetch.is_busy() is False
