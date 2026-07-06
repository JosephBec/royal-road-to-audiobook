"""_download_cover must read EPUB covers from disk, not attempt HTTP on the
root-relative /api/epubs/{id}/cover URL (which httpx rejects as unsupported)."""
import asyncio

import pytest

from tests.epub_fixtures import COVER_BYTES


@pytest.fixture()
def epub_novel_with_cover(tmp_path, monkeypatch):
    import database
    from scrapers import epub_local

    database.init_db()
    covers = tmp_path / "EPUBs" / ".covers"
    covers.mkdir(parents=True)
    monkeypatch.setattr(epub_local, "EPUB_DIR", tmp_path / "EPUBs")
    (covers / "My Book.jpg").write_bytes(COVER_BYTES)

    db = database.SessionLocal()
    novel = database.Novel(
        title="My Book", author="A",
        rr_url=epub_local.novel_url("My Book.epub"),
        cover_url="/api/epubs/1/cover",
    )
    db.add(novel); db.commit()
    novel_id = novel.id
    db.close()

    yield novel_id

    db = database.SessionLocal()
    db.query(database.Novel).filter_by(id=novel_id).delete()
    db.commit(); db.close()


def test_epub_cover_read_from_disk_no_http(epub_novel_with_cover, monkeypatch):
    import export_worker
    import httpx

    def boom(*a, **kw):
        raise AssertionError("must not make an HTTP request for an EPUB cover")
    monkeypatch.setattr(httpx, "AsyncClient", boom)

    cover_bytes, ext = asyncio.run(export_worker._download_cover(epub_novel_with_cover))
    assert cover_bytes == COVER_BYTES
    assert ext == "jpg"


def test_epub_cover_missing_on_disk_returns_none(tmp_path, monkeypatch):
    import database
    from scrapers import epub_local
    import export_worker

    database.init_db()
    (tmp_path / "EPUBs" / ".covers").mkdir(parents=True)
    monkeypatch.setattr(epub_local, "EPUB_DIR", tmp_path / "EPUBs")

    db = database.SessionLocal()
    novel = database.Novel(
        title="No Cover Book", author="A",
        rr_url=epub_local.novel_url("No Cover Book.epub"),
        cover_url="/api/epubs/2/cover",
    )
    db.add(novel); db.commit()
    novel_id = novel.id
    db.close()

    cover_bytes, ext = asyncio.run(export_worker._download_cover(novel_id))
    assert cover_bytes is None
    assert ext == "jpg"

    db = database.SessionLocal()
    db.query(database.Novel).filter_by(id=novel_id).delete()
    db.commit(); db.close()
