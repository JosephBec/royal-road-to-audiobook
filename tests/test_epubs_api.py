"""Upload and cover endpoints (UI-delete test lands in Task 5)."""
import pytest
from fastapi.testclient import TestClient

from tests.epub_fixtures import make_epub, COVER_BYTES


@pytest.fixture()
def client(monkeypatch, tmp_path):
    import export_worker
    import epub_library
    from scrapers import epub_local
    monkeypatch.setattr(export_worker, "start_worker", lambda: None)
    monkeypatch.setattr(epub_library, "start", lambda: None)   # no bg loop in tests
    monkeypatch.setattr(epub_library, "remove_chapter_audio", lambda ids: None)
    lib = tmp_path / "EPUBs"
    lib.mkdir()
    (lib / ".covers").mkdir()
    monkeypatch.setattr(epub_local, "EPUB_DIR", lib)
    epub_library.reset()
    from main import app
    with TestClient(app) as c:
        c.epub_dir = lib
        yield c
    import database
    db = database.SessionLocal()
    for n in db.query(database.Novel).filter(database.Novel.rr_url.like("epub://%")).all():
        db.delete(n)
    db.commit(); db.close()


def _upload(client, tmp_path, name="My Book.epub", **make_kwargs):
    src = tmp_path / "upload-src.epub"
    make_epub(src, **make_kwargs)
    with open(src, "rb") as f:
        return client.post("/api/epubs/upload",
                           files={"file": (name, f, "application/epub+zip")})


def test_upload_registers_book(client, tmp_path):
    resp = _upload(client, tmp_path, title="Uploaded Book", cover=COVER_BYTES)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "Uploaded Book"
    assert body["total_chapters"] == 2
    assert (client.epub_dir / "My Book.epub").exists()

    novels = client.get("/api/novels").json()
    mine = next(n for n in novels if n["id"] == body["id"])
    assert mine["source"] == "epub"
    assert mine["cover_url"] == f"/api/epubs/{body['id']}/cover"
    assert client.get(f"/api/epubs/{body['id']}/cover").status_code == 200


def test_upload_duplicate_name_conflicts(client, tmp_path):
    assert _upload(client, tmp_path).status_code == 201
    assert _upload(client, tmp_path).status_code == 409


def test_upload_invalid_file_rejected(client, tmp_path):
    resp = client.post("/api/epubs/upload",
                       files={"file": ("bad.epub", b"garbage", "application/epub+zip")})
    assert resp.status_code == 400
    assert not (client.epub_dir / "bad.epub").exists()


def test_upload_wrong_extension_rejected(client, tmp_path):
    resp = client.post("/api/epubs/upload",
                       files={"file": ("book.mobi", b"whatever", "application/octet-stream")})
    assert resp.status_code == 400


def test_cover_404_for_non_epub_novel(client):
    import database
    db = database.SessionLocal()
    novel = database.Novel(title="Web", rr_url="https://www.royalroad.com/fiction/424242/web")
    db.add(novel); db.commit()
    nid = novel.id
    db.close()
    assert client.get(f"/api/epubs/{nid}/cover").status_code == 404
    db = database.SessionLocal()
    db.query(database.Novel).filter_by(id=nid).delete()
    db.commit(); db.close()


def test_ui_delete_removes_file_and_cover(client, tmp_path):
    body = _upload(client, tmp_path, cover=COVER_BYTES).json()
    assert (client.epub_dir / "My Book.epub").exists()
    assert (client.epub_dir / ".covers" / "My Book.jpg").exists()

    assert client.delete(f"/api/novels/{body['id']}").status_code == 204
    assert not (client.epub_dir / "My Book.epub").exists()
    assert not (client.epub_dir / ".covers" / "My Book.jpg").exists()
    assert all(n["id"] != body["id"] for n in client.get("/api/novels").json())
