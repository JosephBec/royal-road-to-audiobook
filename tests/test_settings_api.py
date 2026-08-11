"""Settings API: read, update, and the validation rules on each field."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    import export_worker, prefetch, epub_library
    monkeypatch.setattr(export_worker, "start_worker", lambda: None)
    monkeypatch.setattr(prefetch, "start_worker", lambda: None)
    monkeypatch.setattr(epub_library, "start", lambda: None)
    from main import app
    with TestClient(app) as c:
        yield c
    # Restore the shared singleton settings row to defaults for later tests.
    import database
    db = database.SessionLocal()
    s = db.query(database.Settings).first()
    if s:
        s.voice, s.speed, s.playback_mode = "af_heart", 1.0, "full"
        s.theme, s.chapter_sort, s.accent = "dark", "asc", None
        db.commit()
    db.close()


def test_get_settings(client):
    data = client.get("/api/settings").json()
    for key in ("voice", "speed", "playback_mode", "theme", "chapter_sort", "audiobook_dir"):
        assert key in data


def test_update_valid_fields(client):
    resp = client.put("/api/settings", json={"speed": 1.5, "theme": "light",
                                             "playback_mode": "instant", "chapter_sort": "desc"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["speed"] == 1.5
    assert data["theme"] == "light"
    assert data["playback_mode"] == "instant"
    assert data["chapter_sort"] == "desc"


@pytest.mark.parametrize("payload,field", [
    ({"speed": 3.0}, "speed"),
    ({"speed": 0.1}, "speed"),
    ({"playback_mode": "turbo"}, "playback_mode"),
    ({"theme": "sepia"}, "theme"),
    ({"chapter_sort": "sideways"}, "chapter_sort"),
    ({"accent": "purple"}, "accent"),
    ({"accent": "#12345"}, "accent"),
    ({"accent": "#12345g"}, "accent"),
])
def test_update_rejects_invalid(client, payload, field):
    resp = client.put("/api/settings", json=payload)
    assert resp.status_code == 400, f"{field} should be rejected: {resp.text}"


@pytest.mark.parametrize("theme", ["dark", "light", "oled", "warm"])
def test_all_themes_accepted(client, theme):
    resp = client.put("/api/settings", json={"theme": theme})
    assert resp.status_code == 200, resp.text
    assert resp.json()["theme"] == theme


def test_accent_roundtrip_and_clear(client):
    data = client.put("/api/settings", json={"accent": "#3B82F6"}).json()
    assert data["accent"] == "#3b82f6"  # stored lowercased
    assert client.get("/api/settings").json()["accent"] == "#3b82f6"
    # Empty string clears back to the theme default (NULL)
    data = client.put("/api/settings", json={"accent": ""}).json()
    assert data["accent"] is None


def test_plex_url_trailing_slash_stripped(client):
    data = client.put("/api/settings", json={"plex_url": "http://plex.local:32400/"}).json()
    assert data["plex_url"] == "http://plex.local:32400"
