"""The /api/version endpoint reports the running build so you can confirm
which code is live (the tray launches main.py as a child process)."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    import export_worker
    import prefetch
    import epub_library
    monkeypatch.setattr(export_worker, "start_worker", lambda: None)
    monkeypatch.setattr(prefetch, "start_worker", lambda: None)
    monkeypatch.setattr(epub_library, "start", lambda: None)
    from main import app
    with TestClient(app) as c:
        yield c


def test_version_reports_sha_and_start_time(client):
    data = client.get("/api/version").json()
    assert "git_sha" in data
    assert isinstance(data["git_sha"], str) and data["git_sha"]
    # started_at is set at lifespan startup, which TestClient runs
    assert data["started_at"] is not None
