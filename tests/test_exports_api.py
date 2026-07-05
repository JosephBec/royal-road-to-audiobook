import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    import export_worker
    enqueued = []
    monkeypatch.setattr(export_worker, "enqueue", lambda job_id: enqueued.append(job_id))
    monkeypatch.setattr(export_worker, "start_worker", lambda: None)  # no bg task in tests
    from main import app
    with TestClient(app) as c:
        c.enqueued = enqueued
        yield c


@pytest.fixture()
def novel_with_chapters(client):
    import database
    db = database.SessionLocal()
    novel = database.Novel(title="API Novel", author="A",
                           rr_url="https://www.royalroad.com/fiction/666/api")
    db.add(novel); db.commit()
    for i in range(1, 6):
        db.add(database.Chapter(novel_id=novel.id, title=f"C{i}", order=i,
                                rr_url=f"https://www.royalroad.com/fiction/666/api/chapter/{i}/c"))
    db.commit()
    nid = novel.id
    yield nid
    db.query(database.ExportJob).delete()
    db.query(database.Chapter).filter_by(novel_id=nid).delete()
    db.query(database.Novel).filter_by(id=nid).delete()
    db.commit(); db.close()


def test_create_export_job(client, novel_with_chapters):
    resp = client.post(f"/api/novels/{novel_with_chapters}/export",
                       json={"start_order": 2, "end_order": 4,
                             "voice": "af_heart", "speed": 1.25})
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    assert client.enqueued == [job_id]

    jobs = client.get("/api/exports").json()["jobs"]
    assert jobs[0]["id"] == job_id
    assert jobs[0]["status"] == "queued"
    assert jobs[0]["chapters_total"] == 3


def test_duplicate_job_conflicts(client, novel_with_chapters):
    body = {"start_order": 1, "end_order": 5, "voice": "af_heart", "speed": 1.0}
    assert client.post(f"/api/novels/{novel_with_chapters}/export", json=body).status_code == 200
    assert client.post(f"/api/novels/{novel_with_chapters}/export", json=body).status_code == 409


def test_bad_range_rejected(client, novel_with_chapters):
    resp = client.post(f"/api/novels/{novel_with_chapters}/export",
                       json={"start_order": 4, "end_order": 2,
                             "voice": "af_heart", "speed": 1.0})
    assert resp.status_code == 400


def test_cancel_and_retry(client, novel_with_chapters):
    job_id = client.post(f"/api/novels/{novel_with_chapters}/export",
                         json={"start_order": 1, "end_order": 2,
                               "voice": "af_heart", "speed": 1.0}).json()["job_id"]
    assert client.post(f"/api/exports/{job_id}/cancel").json()["status"] == "canceled"
    assert client.post(f"/api/exports/{job_id}/retry").json()["status"] == "queued"


def test_settings_roundtrip_new_fields(client):
    resp = client.put("/api/settings", json={"plex_url": "http://localhost:32400",
                                             "plex_token": "tok"})
    assert resp.status_code == 200
    data = client.get("/api/settings").json()
    assert data["plex_url"] == "http://localhost:32400"
    assert data["audiobook_dir"].endswith("Audiobooks")


def test_plex_libraries_unconfigured(client):
    client.put("/api/settings", json={"plex_url": "", "plex_token": ""})
    assert client.get("/api/plex/libraries").status_code == 400
