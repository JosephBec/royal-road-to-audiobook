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

    async def fake_synth(chapter_id, text, voice, speed, engine_name=None):
        synth_calls.append(chapter_id)
        path = tts.temp_path_for_chapter(chapter_id)
        path.write_bytes(b"wav")
        return path
    monkeypatch.setattr(prefetch.tts, "synthesize_chapter_to_file", fake_synth)

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


def test_head_start_is_skipped_when_nothing_is_queued(pf_env, monkeypatch):
    """An idle worker must not spin the GPU up on every wake."""
    prefetch, _, _, _, _ = pf_env
    calls = []

    async def fake_head_start():
        calls.append(1)
    monkeypatch.setattr(prefetch, "head_start_pass", fake_head_start)

    asyncio.run(prefetch.drain_once())
    assert calls == []


def test_is_busy_false_after_drain(pf_env):
    prefetch, tts, synth_calls, _, _ = pf_env
    prefetch.enqueue(_targets(1, 2), "af_heart")
    assert prefetch.is_busy() is True
    asyncio.run(prefetch.drain_once())
    assert prefetch.is_busy() is False
