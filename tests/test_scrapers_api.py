"""The supported-sites list comes from the scraper registry, not hardcoded UI."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    import export_worker
    monkeypatch.setattr(export_worker, "start_worker", lambda: None)
    from main import app
    with TestClient(app) as c:
        yield c


def test_scrapers_reflect_registry(client):
    data = client.get("/api/scrapers").json()
    names = {s["name"] for s in data["scrapers"]}
    assert {"Royal Road", "Ranobes"} <= names
    for s in data["scrapers"]:
        assert isinstance(s["patterns"], list) and s["patterns"]
