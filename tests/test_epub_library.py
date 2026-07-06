"""Folder sync: files appearing/vanishing/changing drive the library."""
import asyncio
import os

import pytest

from tests.epub_fixtures import make_epub, LONG_PARA, COVER_BYTES


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import database
    database.init_db()
    from scrapers import epub_local
    import epub_library
    lib = tmp_path / "EPUBs"
    lib.mkdir()
    (lib / ".covers").mkdir()
    monkeypatch.setattr(epub_local, "EPUB_DIR", lib)
    removed_audio = []
    monkeypatch.setattr(epub_library, "remove_chapter_audio",
                        lambda ids: removed_audio.append(set(ids)))
    epub_library.reset()
    yield lib, removed_audio
    db = database.SessionLocal()
    for n in db.query(database.Novel).filter(database.Novel.rr_url.like("epub://%")).all():
        db.delete(n)
    db.commit(); db.close()


def _sync():
    import epub_library
    asyncio.run(epub_library.sync_once())


def _epub_novels():
    """Snapshot epub novels as plain dicts (session-independent)."""
    import database
    db = database.SessionLocal()
    try:
        rows = db.query(database.Novel).filter(database.Novel.rr_url.like("epub://%")).all()
        return [{"id": n.id, "title": n.title, "total_chapters": n.total_chapters,
                 "cover_url": n.cover_url,
                 "chapter_ids": [c.id for c in sorted(n.chapters, key=lambda c: c.order)]}
                for n in rows]
    finally:
        db.close()


def test_new_file_registers_after_two_stable_polls(env):
    lib, _ = env
    make_epub(lib / "Book One.epub", title="Book One", cover=COVER_BYTES)
    _sync()
    assert _epub_novels() == []          # first sighting: waiting for stability
    _sync()
    novels = _epub_novels()
    assert len(novels) == 1
    assert novels[0]["title"] == "Book One"
    assert novels[0]["total_chapters"] == 2
    assert novels[0]["cover_url"] == f"/api/epubs/{novels[0]['id']}/cover"
    assert (lib / ".covers" / "Book One.jpg").exists()


def test_file_still_copying_is_not_registered(env):
    lib, _ = env
    path = make_epub(lib / "Partial.epub")
    _sync()
    with open(path, "ab") as f:          # size changes between polls
        f.write(b"\x00" * 10)
    _sync()
    assert _epub_novels() == []
    _sync()
    assert len(_epub_novels()) == 1


def test_removed_file_deletes_novel_progress_and_audio(env):
    lib, removed_audio = env
    make_epub(lib / "Gone.epub", cover=COVER_BYTES)
    _sync(); _sync()
    novel = _epub_novels()[0]
    import database
    db = database.SessionLocal()
    db.add(database.Progress(novel_id=novel["id"], chapter_id=novel["chapter_ids"][0]))
    db.commit(); db.close()

    (lib / "Gone.epub").unlink()
    _sync()
    assert _epub_novels() == []
    assert removed_audio and removed_audio[0] == set(novel["chapter_ids"])
    assert not (lib / ".covers" / "Gone.jpg").exists()
    db = database.SessionLocal()
    assert db.query(database.Progress).filter_by(novel_id=novel["id"]).first() is None
    db.close()


def test_replaced_file_resyncs_chapters_keeps_progress(env):
    lib, _ = env
    path = make_epub(lib / "Series.epub",
                     chapters=[("Ch 1", [LONG_PARA]), ("Ch 2", [LONG_PARA])])
    _sync(); _sync()
    novel = _epub_novels()[0]
    import database
    db = database.SessionLocal()
    db.add(database.Progress(novel_id=novel["id"], chapter_id=novel["chapter_ids"][1]))
    db.commit(); db.close()

    make_epub(lib / "Series.epub",
              chapters=[("Ch 1", [LONG_PARA]), ("Ch 2", [LONG_PARA]), ("Ch 3", [LONG_PARA])])
    os.utime(path)                        # ensure mtime moves even on coarse clocks
    _sync()
    novel2 = _epub_novels()[0]
    assert novel2["id"] == novel["id"]    # same row — progress preserved
    assert novel2["total_chapters"] == 3
    db = database.SessionLocal()
    prog = db.query(database.Progress).filter_by(novel_id=novel["id"]).first()
    assert prog is not None and prog.chapter_id == novel["chapter_ids"][1]
    db.close()


def test_corrupt_file_skipped_without_crash(env):
    lib, _ = env
    (lib / "junk.epub").write_bytes(b"not really an epub")
    _sync(); _sync(); _sync()
    assert _epub_novels() == []
