"""The test suite must never be able to touch the real audio cache.

Starting the app applies the retention policy, and retention deletes every
chapter file the database cannot account for. A test database accounts for
nothing, so ten modules that do `with TestClient(app)` were between them
deleting the entire cache on every run — hours of rendered audio, silently,
with the loss put down to server restarts.

This is guarded here rather than left to conftest because the damage is
invisible: nothing fails, no test goes red, the audio is simply gone.
"""
from pathlib import Path


def test_temp_audio_is_not_the_real_directory():
    import tts
    real = Path("./temp_audio").resolve()
    assert tts.TEMP_DIR.resolve() != real, (
        "tests are pointed at the real audio cache; retention will delete it")


def test_starting_the_app_leaves_the_real_cache_alone():
    """The end-to-end version of the same guarantee."""
    from fastapi.testclient import TestClient
    import tts
    from main import app

    real = Path("./temp_audio")
    real.mkdir(exist_ok=True)
    sentinel = real / "chapter_99999999.wav"
    sentinel.write_bytes(b"not yours to delete")
    try:
        with TestClient(app) as client:     # runs lifespan → retention
            client.get("/api/version")
        assert sentinel.exists(), \
            "starting the app deleted real cached audio"
    finally:
        sentinel.unlink(missing_ok=True)

    assert tts.TEMP_DIR.resolve() != real.resolve()
