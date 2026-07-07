"""Single render-ahead worker: dedups, skips rendered chapters, runs cleanup.

No GPU/network — synthesis and scraping are faked.
"""
import asyncio

import pytest


@pytest.fixture()
def pf_env(tmp_path, monkeypatch):
    import tts
    import prefetch
    monkeypatch.setattr(tts, "TEMP_DIR", tmp_path)

    synth_calls = []

    async def fake_synth(chapter_id, text, voice, speed):
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


def test_is_busy_false_after_drain(pf_env):
    prefetch, tts, synth_calls, _, _ = pf_env
    prefetch.enqueue(_targets(1, 2), "af_heart")
    assert prefetch.is_busy() is True
    asyncio.run(prefetch.drain_once())
    assert prefetch.is_busy() is False
