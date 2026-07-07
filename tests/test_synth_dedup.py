"""Concurrent synthesis of the same chapter must render only once.

Two callers (e.g. playback + the prefetch worker) can request the same
chapter before either finishes. Without in-flight de-duplication both would
synthesize it, doubling GPU work — the root cause of the prefetch queue
falling behind.
"""
import asyncio

import numpy as np
import pytest


@pytest.fixture()
def tts_env(tmp_path, monkeypatch):
    import tts
    monkeypatch.setattr(tts, "TEMP_DIR", tmp_path)

    calls = {"n": 0}

    def fake_blocking(pipeline, text, voice, speed):
        calls["n"] += 1
        import time
        time.sleep(0.2)  # hold long enough for a concurrent call to arrive
        return [np.zeros(2400, dtype=np.float32)]

    async def fake_pipeline(voice="af_heart"):
        return object()

    monkeypatch.setattr(tts, "_synthesize_text_blocking", fake_blocking)
    monkeypatch.setattr(tts, "get_pipeline", fake_pipeline)
    return tts, calls


def test_concurrent_same_chapter_synthesizes_once(tts_env):
    tts, calls = tts_env

    async def go():
        return await asyncio.gather(
            tts.synthesize_chapter_to_file(555, "body one", "af_heart", 1.0),
            tts.synthesize_chapter_to_file(555, "body two", "af_heart", 1.0),
        )

    paths = asyncio.run(go())

    assert calls["n"] == 1, f"expected one synthesis, got {calls['n']}"
    assert paths[0] == paths[1] == tts.temp_path_for_chapter(555)
    assert paths[0].exists()


def test_sequential_distinct_chapters_each_synthesize(tts_env):
    tts, calls = tts_env

    async def go():
        await tts.synthesize_chapter_to_file(601, "a", "af_heart", 1.0)
        await tts.synthesize_chapter_to_file(602, "b", "af_heart", 1.0)

    asyncio.run(go())
    assert calls["n"] == 2
