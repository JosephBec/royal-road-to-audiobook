"""Cover URLs must not survive a novel id being recycled.

SQLite reuses row ids: delete the highest-id novel and the next one added takes
the same id, so `/api/epubs/16/cover` legitimately means two different books
over time. Without a content-derived token and a validator, the browser serves
the previous book's artwork from cache.
"""
import pytest
from fastapi.testclient import TestClient

from tests.epub_fixtures import make_epub, COVER_BYTES

OTHER_COVER = b"\xff\xd8\xff\xe0" + b"\x11" * 2000  # different bytes, same shape


@pytest.fixture()
def client(monkeypatch, tmp_path):
    import export_worker
    import epub_library
    from scrapers import epub_local
    monkeypatch.setattr(export_worker, "start_worker", lambda: None)
    monkeypatch.setattr(epub_library, "start", lambda: None)
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


def _upload(client, tmp_path, name, title, cover):
    src = tmp_path / f"src-{name}"
    make_epub(src, title=title, cover=cover)
    with open(src, "rb") as f:
        resp = client.post("/api/epubs/upload",
                           files={"file": (name, f, "application/epub+zip")})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _cover_url(client, novel_id):
    novels = client.get("/api/novels").json()
    return next(n["cover_url"] for n in novels if n["id"] == novel_id)


def test_cover_url_carries_a_content_token(client, tmp_path):
    book = _upload(client, tmp_path, "a.epub", "Book A", COVER_BYTES)
    assert "?v=" in _cover_url(client, book["id"])


def test_different_covers_get_different_tokens(client, tmp_path):
    a = _upload(client, tmp_path, "a.epub", "Book A", COVER_BYTES)
    b = _upload(client, tmp_path, "b.epub", "Book B", OTHER_COVER)
    assert _cover_url(client, a["id"]) != _cover_url(client, b["id"])
    token_a = _cover_url(client, a["id"]).split("?v=")[1]
    token_b = _cover_url(client, b["id"]).split("?v=")[1]
    assert token_a != token_b, "same token would let one book's cover cache over the other"


def test_recycled_novel_id_does_not_reuse_the_cover_url(client, tmp_path):
    """The reported bug: remove a book, add the next one, see the old cover."""
    first = _upload(client, tmp_path, "first.epub", "First Book", COVER_BYTES)
    first_url = _cover_url(client, first["id"])

    assert client.delete(f"/api/novels/{first['id']}").status_code == 204
    (client.epub_dir / "first.epub").unlink(missing_ok=True)

    second = _upload(client, tmp_path, "second.epub", "Second Book", OTHER_COVER)
    second_url = _cover_url(client, second["id"])

    if second["id"] == first["id"]:
        # id was recycled — exactly the case that used to serve a stale image
        assert second_url != first_url, "recycled id must not reuse the cover URL"
    assert second_url.split("?v=")[1] != first_url.split("?v=")[1]


def test_cover_response_is_revalidatable(client, tmp_path):
    book = _upload(client, tmp_path, "a.epub", "Book A", COVER_BYTES)
    resp = client.get(f"/api/epubs/{book['id']}/cover")
    assert resp.status_code == 200
    assert resp.headers["etag"]
    assert "no-cache" in resp.headers["cache-control"]


def test_matching_etag_returns_304(client, tmp_path):
    book = _upload(client, tmp_path, "a.epub", "Book A", COVER_BYTES)
    etag = client.get(f"/api/epubs/{book['id']}/cover").headers["etag"]
    resp = client.get(f"/api/epubs/{book['id']}/cover",
                      headers={"If-None-Match": etag})
    assert resp.status_code == 304
    assert not resp.content


def test_stale_etag_returns_the_image(client, tmp_path):
    book = _upload(client, tmp_path, "a.epub", "Book A", COVER_BYTES)
    resp = client.get(f"/api/epubs/{book['id']}/cover",
                      headers={"If-None-Match": '"stale00000000"'})
    assert resp.status_code == 200
    assert resp.content


def test_glob_escape_handles_bracketed_titles():
    """Filenames really do contain [ ] ? * — those must match literally."""
    from scrapers.epub_local import glob_escape
    assert glob_escape("1% Lifesteal [Book 3]") == "1% Lifesteal [[]Book 3[]]"
