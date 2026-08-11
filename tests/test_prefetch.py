"""The cache sweep worker: renders the plan in order, and always terminates.

No GPU and no network — synthesis and scraping are faked.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def pf_env(tmp_path, monkeypatch):
    import database
    import prefetch
    import tts

    database.init_db()
    monkeypatch.setattr(tts, "TEMP_DIR", tmp_path)

    rendered = []

    async def fake_synth(chapter_id, text, voice, speed, engine_name=None,
                         chunk_voices=None, max_seconds=None,
                         yield_to_interactive=False):
        rendered.append({"chapter_id": chapter_id, "max_seconds": max_seconds,
                         "voice": voice, "engine": engine_name,
                         "yield_to_interactive": yield_to_interactive})
        # The real head start leaves this much audio recorded on disk, which is
        # what makes the chapter satisfied and stops the sweep re-choosing it.
        tts.record_segment_duration(chapter_id, 0, max_seconds or 999.0)
    monkeypatch.setattr(prefetch.tts, "synthesize_chapter_streaming", fake_synth)

    async def unexpected(*a, **k):
        raise AssertionError("the sweep must never render a whole chapter")
    monkeypatch.setattr(prefetch.tts, "synthesize_chapter_to_file", unexpected)

    class FakeScraper:
        async def scrape_chapter_text(self, url):
            return f"text for {url}"
    monkeypatch.setattr(prefetch, "get_scraper_for_url", lambda url: FakeScraper())

    async def no_wait():
        return
    monkeypatch.setattr(prefetch, "_wait_for_interactive_idle", no_wait)

    cleanups = []
    monkeypatch.setattr(prefetch.tts, "cleanup_temp_files",
                        lambda keep, expiring=None: cleanups.append((keep, expiring)))

    prefetch.reset()
    db = database.SessionLocal()
    # Clean going in as well as coming out: the sweep plans over the whole
    # library, so a novel another test module left behind would be swept too.
    _wipe(db, database)
    yield prefetch, tts, db, database, rendered, cleanups
    _wipe(db, database)
    db.close()


def _wipe(session, database):
    session.query(database.Progress).delete()
    session.query(database.Chapter).delete()
    session.query(database.Novel).delete()
    session.commit()


def _reading(db, database, seed, chapters=10, at=5, favorite=False):
    """A novel with the reader parked on chapter `at` (1-based)."""
    novel = database.Novel(title=seed, rr_url=f"https://rr.test/{seed}",
                           favorite=favorite)
    db.add(novel)
    db.flush()
    rows = []
    for order in range(1, chapters + 1):
        ch = database.Chapter(novel_id=novel.id, title=f"{seed} c{order}",
                              order=order, text=f"body {order}",
                              rr_url=f"https://rr.test/{seed}/{order}")
        db.add(ch)
        rows.append(ch)
    db.flush()
    db.add(database.Progress(novel_id=novel.id, chapter_id=rows[at - 1].id,
                             updated_at=datetime.now(timezone.utc)))
    db.commit()
    return novel, rows


def test_sweep_renders_the_plan_in_priority_order(pf_env):
    prefetch, tts, db, database, rendered, _ = pf_env
    _, chs = _reading(db, database, "plan", at=5)

    asyncio.run(prefetch.sweep_once())

    assert [r["chapter_id"] for r in rendered] == [
        chs[4].id, chs[5].id, chs[3].id, chs[6].id, chs[7].id]


def test_sweep_only_ever_renders_the_opening(pf_env):
    """Nothing renders a whole chapter in the background any more.

    A full render is twenty minutes of GPU for a chapter that may never be
    opened, and it used to run ahead of the openings that actually shorten the
    wait. Two minutes covers the model load and the first chunk; past that the
    engine outruns playback.
    """
    prefetch, tts, db, database, rendered, _ = pf_env
    _reading(db, database, "cap", at=1)

    asyncio.run(prefetch.sweep_once())

    import cache_policy
    assert rendered, "something should have been rendered"
    assert all(r["max_seconds"] == cache_policy.HEAD_START_SECONDS for r in rendered)
    assert all(r["yield_to_interactive"] for r in rendered)


def test_sweep_skips_chapters_that_already_have_their_opening(pf_env):
    prefetch, tts, db, database, rendered, _ = pf_env
    _, chs = _reading(db, database, "warm", at=1)
    tts.record_segment_duration(chs[0].id, 0, 130.0)      # opening already there
    tts.temp_path_for_chapter(chs[1].id).write_bytes(b"complete")

    asyncio.run(prefetch.sweep_once())

    done = [r["chapter_id"] for r in rendered]
    assert chs[0].id not in done and chs[1].id not in done
    assert done == [chs[2].id, chs[3].id]


def test_a_target_that_cannot_be_rendered_does_not_loop_forever(pf_env, monkeypatch):
    """The sweep picks targets by "does this chapter have its opening yet".

    A render that ends without producing one — a scrape that fails, a synthesis
    error — answers no, so the planner would hand back the same chapter on the
    next iteration and the worker would spin on it for as long as the process
    lived. Confirming the outcome against the planner's own condition is what
    bounds the loop.
    """
    prefetch, tts, db, database, rendered, _ = pf_env
    _reading(db, database, "broken", at=1)

    async def produces_nothing(chapter_id, text, voice, speed, engine_name=None,
                               chunk_voices=None, max_seconds=None,
                               yield_to_interactive=False):
        rendered.append({"chapter_id": chapter_id})
    monkeypatch.setattr(prefetch.tts, "synthesize_chapter_streaming",
                        produces_nothing)

    async def bounded():
        return await asyncio.wait_for(prefetch.sweep_once(), timeout=5)
    assert asyncio.run(bounded()) == 0

    attempts = [r["chapter_id"] for r in rendered]
    assert len(attempts) == len(set(attempts)), "each target attempted once per pass"


def test_a_scrape_failure_defers_the_chapter(pf_env, monkeypatch):
    prefetch, tts, db, database, rendered, _ = pf_env
    _reading(db, database, "noscrape", at=1)
    for ch in db.query(database.Chapter).all():
        ch.text = None
    db.commit()
    monkeypatch.setattr(prefetch, "get_scraper_for_url", lambda url: None)

    async def bounded():
        return await asyncio.wait_for(prefetch.sweep_once(), timeout=5)

    assert asyncio.run(bounded()) == 0
    assert rendered == []


def test_retention_runs_after_the_sweep(pf_env):
    prefetch, tts, db, database, rendered, cleanups = pf_env
    _, chs = _reading(db, database, "retain", at=5)

    asyncio.run(prefetch.sweep_once())

    assert len(cleanups) == 1
    keep, _expiring = cleanups[0]
    assert chs[4].id in keep


def test_retention_protects_a_chapter_that_is_still_rendering(pf_env, monkeypatch):
    """Progress lands after playback starts, so a chapter can be mid-render and
    not yet anywhere in the plan. Sweeping its segments away mid-render leaves
    a chapter that is half one take and half another.

    Exercises the real cleanup_temp_files: the protection lives inside it, so a
    capture of the arguments would prove nothing.
    """
    prefetch, tts, db, database, rendered, cleanups = pf_env
    _reading(db, database, "inflight", at=5)
    monkeypatch.setattr(tts, "active_chapter_ids", lambda: {999_111})
    monkeypatch.setattr(prefetch.tts, "cleanup_temp_files", tts.cleanup_temp_files)
    victim = tts.temp_path_for_chapter(999_111)
    victim.write_bytes(b"mid-render segment data")

    asyncio.run(prefetch.sweep_once())

    assert victim.exists(), "retention deleted a chapter that was still rendering"


def test_a_finished_head_start_is_not_active(pf_env):
    """A head start ends with its streaming state still marked incomplete —
    that is what makes it resumable. Treating incomplete as active made every
    head-started chapter permanently unsweepable until a restart, so archived
    novels never actually released their audio."""
    prefetch, tts, db, database, rendered, _ = pf_env
    tts._streaming_state[777] = {"complete": False, "segments": [],
                                 "total_duration": 120.0}

    assert 777 not in tts.active_chapter_ids()
    tts._streaming_state.pop(777, None)


def test_the_worker_sweeps_on_an_idle_tick(pf_env, monkeypatch):
    """Nothing enqueues on a quiet server, and idle time is exactly when this
    work should happen — its whole purpose is to be done before play is pressed."""
    prefetch, *_ = pf_env
    passes = []

    async def fake_sweep():
        passes.append(1)
        if len(passes) >= 2:
            raise asyncio.CancelledError
        return 0
    monkeypatch.setattr(prefetch, "sweep_once", fake_sweep)
    monkeypatch.setattr(prefetch, "IDLE_TICK_SECONDS", 0.01)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await prefetch._worker_loop()
    asyncio.run(run())

    assert len(passes) == 2


def test_request_sweep_does_not_wait_for_the_tick(pf_env, monkeypatch):
    prefetch, *_ = pf_env
    passes = []

    async def fake_sweep():
        passes.append(1)
        raise asyncio.CancelledError
    monkeypatch.setattr(prefetch, "sweep_once", fake_sweep)
    monkeypatch.setattr(prefetch, "IDLE_TICK_SECONDS", 30)   # far longer than the test

    async def run():
        prefetch.request_sweep()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(prefetch._worker_loop(), timeout=2)
    asyncio.run(run())

    assert passes == [1]


def test_is_busy_reflects_pending_and_active_work(pf_env):
    prefetch, *_ = pf_env

    assert prefetch.is_busy() is False
    prefetch.request_sweep()
    assert prefetch.is_busy() is True, "a requested sweep is work the export waits on"


def test_startup_sweeps_without_waiting_for_a_tick(pf_env, monkeypatch):
    """A restart is when the cache is most likely to be behind: anything caught
    mid-render is short of its opening. Idling for the first two minutes is two
    minutes the next press of play might pay for."""
    prefetch, *_ = pf_env
    passes = []

    async def fake_sweep():
        passes.append(1)
        raise asyncio.CancelledError
    monkeypatch.setattr(prefetch, "sweep_once", fake_sweep)
    monkeypatch.setattr(prefetch, "IDLE_TICK_SECONDS", 30)

    async def run():
        prefetch.start_worker()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(prefetch._worker_task, timeout=2)
    asyncio.run(run())

    assert passes == [1]
