"""Voice demo endpoint: generate-once caching, unknown-voice 404."""
import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    import export_worker
    monkeypatch.setattr(export_worker, "start_worker", lambda: None)
    import tts
    calls = {"n": 0}

    async def fake_batch(text, voice, speed):
        calls["n"] += 1
        return [np.zeros(2400, dtype=np.float32)]
    monkeypatch.setattr(tts, "synthesize_batch", fake_batch)

    import main
    monkeypatch.setattr(main, "VOICE_DEMO_DIR", tmp_path / "voice_demos")
    with TestClient(main.app) as c:
        c.calls = calls
        yield c


def test_demo_generated_then_cached(client):
    r1 = client.get("/api/voices/af_heart/demo")
    assert r1.status_code == 200
    assert r1.headers["content-type"].startswith("audio/")
    assert len(r1.content) > 44  # more than a bare WAV header

    r2 = client.get("/api/voices/af_heart/demo")
    assert r2.status_code == 200
    assert client.calls["n"] == 1  # second request served from disk cache


def test_unknown_voice_404(client):
    assert client.get("/api/voices/definitely_not_a_voice/demo").status_code == 404
    assert client.calls["n"] == 0
