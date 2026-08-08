"""Engine selection through the API: validation, voice coercion, cache invalidation."""
import pytest
from fastapi.testclient import TestClient

from export_worker import _atempo_filter


@pytest.fixture()
def client(monkeypatch):
    import export_worker, prefetch, epub_library
    monkeypatch.setattr(export_worker, "start_worker", lambda: None)
    monkeypatch.setattr(prefetch, "start_worker", lambda: None)
    monkeypatch.setattr(epub_library, "start", lambda: None)
    from main import app
    with TestClient(app) as c:
        yield c
    # Restore the shared singleton settings row for later tests.
    import database
    db = database.SessionLocal()
    s = db.query(database.Settings).first()
    if s:
        s.engine, s.voice, s.speed = "kokoro", "af_heart", 1.0
        s.playback_mode, s.theme, s.chapter_sort = "full", "dark", "asc"
        db.commit()
    db.close()


# ----- ffmpeg speed fallback for engines with no speed parameter -----

@pytest.mark.parametrize("speed,expected", [
    (1.5, "atempo=1.5"),
    (0.75, "atempo=0.75"),
    (2.0, "atempo=2"),
    (0.5, "atempo=0.5"),
])
def test_atempo_single_filter_in_range(speed, expected):
    assert _atempo_filter(speed) == expected


def test_atempo_chains_outside_single_filter_range():
    """atempo only accepts 0.5–2.0, so 3x has to become a product."""
    chain = _atempo_filter(3.0)
    assert chain.count("atempo") == 2
    product = 1.0
    for part in chain.split(","):
        product *= float(part.split("=")[1])
    assert product == pytest.approx(3.0)


def test_atempo_chains_below_range():
    chain = _atempo_filter(0.25)
    product = 1.0
    for part in chain.split(","):
        product *= float(part.split("=")[1])
    assert product == pytest.approx(0.25)


# ----- settings API -----

def test_engine_defaults_to_kokoro(client):
    body = client.get("/api/settings").json()
    assert body["engine"] == "kokoro"


def test_engines_endpoint_lists_both(client):
    body = client.get("/api/engines").json()
    names = [e["name"] for e in body["engines"]]
    assert "kokoro" in names and "chatterbox" in names
    assert body["default_engine"] == "kokoro"
    # The default engine is listed first so the picker opens on it.
    assert names[0] == "kokoro"


def test_unknown_engine_is_rejected(client):
    r = client.put("/api/settings", json={"engine": "nope"})
    assert r.status_code == 400
    assert "nope" in r.json()["detail"]


def test_switching_engine_coerces_a_stale_voice(client):
    """af_heart means nothing to Chatterbox; the server must snap to its default."""
    client.put("/api/settings", json={"engine": "kokoro", "voice": "af_heart"})
    body = client.put("/api/settings", json={"engine": "chatterbox"}).json()
    assert body["engine"] == "chatterbox"
    assert body["voice"] == "cb_builtin"


def test_switching_back_restores_a_valid_kokoro_voice(client):
    client.put("/api/settings", json={"engine": "chatterbox"})
    body = client.put("/api/settings", json={"engine": "kokoro"}).json()
    assert body["engine"] == "kokoro"
    assert body["voice"].startswith(("af_", "am_", "bf_", "bm_"))


def test_voices_endpoint_is_engine_scoped(client):
    kokoro = client.get("/api/voices?engine=kokoro").json()
    chatterbox = client.get("/api/voices?engine=chatterbox").json()
    assert {v["id"] for v in kokoro["voices"]} & {v["id"] for v in chatterbox["voices"]} == set()
    assert kokoro["supports_speed"] is True
    assert chatterbox["supports_speed"] is False
