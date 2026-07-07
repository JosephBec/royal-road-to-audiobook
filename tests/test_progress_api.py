"""Progress API: defaults, create, update, and 404s."""
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


@pytest.fixture()
def novel_with_chapters(client):
    import database
    db = database.SessionLocal()
    novel = database.Novel(title="Prog Novel", author="A",
                           rr_url="https://www.royalroad.com/fiction/888/prog")
    db.add(novel); db.commit()
    ids = []
    for i in range(1, 4):
        ch = database.Chapter(novel_id=novel.id, title=f"C{i}", order=i,
                              rr_url=f"https://www.royalroad.com/fiction/888/prog/chapter/{i}/c")
        db.add(ch); db.commit(); ids.append(ch.id)
    nid = novel.id
    yield nid, ids
    db.query(database.Progress).filter_by(novel_id=nid).delete()
    db.query(database.Chapter).filter_by(novel_id=nid).delete()
    db.query(database.Novel).filter_by(id=nid).delete()
    db.commit(); db.close()


def test_get_progress_defaults_when_none(client, novel_with_chapters):
    nid, _ = novel_with_chapters
    data = client.get(f"/api/progress/{nid}").json()
    assert data["chapter_id"] is None
    assert data["position_seconds"] == 0.0
    assert data["updated_at"] is None


def test_put_creates_then_updates(client, novel_with_chapters):
    nid, ids = novel_with_chapters
    r1 = client.put(f"/api/progress/{nid}", json={"chapter_id": ids[0], "position_seconds": 12.5})
    assert r1.status_code == 200, r1.text
    assert r1.json()["chapter_id"] == ids[0]
    assert r1.json()["chapter_order"] == 1

    r2 = client.put(f"/api/progress/{nid}", json={"chapter_id": ids[2], "position_seconds": 3.0})
    assert r2.json()["chapter_id"] == ids[2]
    assert r2.json()["chapter_order"] == 3

    got = client.get(f"/api/progress/{nid}").json()
    assert got["chapter_id"] == ids[2]
    assert got["position_seconds"] == 3.0


def test_put_unknown_novel_404(client):
    resp = client.put("/api/progress/99999", json={"chapter_id": 1, "position_seconds": 0})
    assert resp.status_code == 404


def test_put_unknown_chapter_404(client, novel_with_chapters):
    nid, _ = novel_with_chapters
    resp = client.put(f"/api/progress/{nid}", json={"chapter_id": 99999, "position_seconds": 0})
    assert resp.status_code == 404


def test_get_unknown_novel_404(client):
    assert client.get("/api/progress/99999").status_code == 404
