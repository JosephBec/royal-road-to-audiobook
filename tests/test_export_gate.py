from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def fresh_db():
    import database
    database.init_db()
    db = database.SessionLocal()
    yield db, database
    db.query(database.Progress).delete()
    db.query(database.Chapter).delete()
    db.query(database.Novel).delete()
    db.commit(); db.close()


def _novel_with_chapters(db, database, n=5, url_seed="gate1"):
    novel = database.Novel(title="G", rr_url=f"https://www.royalroad.com/fiction/888/{url_seed}")
    db.add(novel); db.commit()
    chapters = []
    for i in range(1, n + 1):
        ch = database.Chapter(novel_id=novel.id, title=f"C{i}", order=i,
                              rr_url=f"https://www.royalroad.com/fiction/888/{url_seed}/chapter/{i}/c")
        db.add(ch); chapters.append(ch)
    db.commit()
    return novel, chapters


def test_no_listeners_no_debt(fresh_db):
    db, database = fresh_db
    import export_worker
    assert export_worker._active_listener_debt(db) is False


def test_recent_listener_with_cold_cache_is_debt(fresh_db, tmp_path, monkeypatch):
    db, database = fresh_db
    novel, chapters = _novel_with_chapters(db, database, url_seed="gate2")
    db.add(database.Progress(novel_id=novel.id, chapter_id=chapters[0].id,
                             updated_at=datetime.now(timezone.utc)))
    db.commit()
    import export_worker, tts
    monkeypatch.setattr(tts, "TEMP_DIR", tmp_path)  # hermetic: empty cache dir
    assert export_worker._active_listener_debt(db) is True


def test_recent_listener_with_warm_cache_is_not_debt(fresh_db, tmp_path, monkeypatch):
    db, database = fresh_db
    novel, chapters = _novel_with_chapters(db, database, url_seed="gate3")
    db.add(database.Progress(novel_id=novel.id, chapter_id=chapters[0].id,
                             updated_at=datetime.now(timezone.utc)))
    db.commit()
    import export_worker, tts
    monkeypatch.setattr(tts, "TEMP_DIR", tmp_path)
    for ch in chapters[:4]:  # current + next 3
        tts.temp_path_for_chapter(ch.id).write_bytes(b"x")
    assert export_worker._active_listener_debt(db) is False


def test_stale_listener_is_not_debt(fresh_db):
    db, database = fresh_db
    novel, chapters = _novel_with_chapters(db, database, url_seed="gate4")
    db.add(database.Progress(novel_id=novel.id, chapter_id=chapters[0].id,
                             updated_at=datetime.now(timezone.utc) - timedelta(seconds=600)))
    db.commit()
    import export_worker
    assert export_worker._active_listener_debt(db) is False


def test_may_proceed_blocks_on_interactive(fresh_db, monkeypatch):
    db, database = fresh_db
    import export_worker, tts
    monkeypatch.setattr(tts, "interactive_busy", lambda: True)
    assert export_worker._export_may_proceed(db) is False
    monkeypatch.setattr(tts, "interactive_busy", lambda: False)
    assert export_worker._export_may_proceed(db) is True


def test_may_proceed_blocks_on_favorites_sync(fresh_db, monkeypatch):
    db, database = fresh_db
    import export_worker, tts
    monkeypatch.setattr(tts, "interactive_busy", lambda: False)
    monkeypatch.setattr(export_worker.library_sync, "is_running", lambda: True)
    assert export_worker._export_may_proceed(db) is False
    monkeypatch.setattr(export_worker.library_sync, "is_running", lambda: False)
    assert export_worker._export_may_proceed(db) is True


def test_may_proceed_blocks_on_listener_debt(fresh_db, tmp_path, monkeypatch):
    db, database = fresh_db
    novel, chapters = _novel_with_chapters(db, database, url_seed="gate5")
    db.add(database.Progress(novel_id=novel.id, chapter_id=chapters[0].id,
                             updated_at=datetime.now(timezone.utc)))
    db.commit()
    import export_worker, tts
    monkeypatch.setattr(tts, "interactive_busy", lambda: False)
    monkeypatch.setattr(export_worker.library_sync, "is_running", lambda: False)
    monkeypatch.setattr(tts, "TEMP_DIR", tmp_path)  # empty -> cold cache -> debt
    assert export_worker._export_may_proceed(db) is False
    for ch in chapters[:4]:  # current + next 3
        tts.temp_path_for_chapter(ch.id).write_bytes(b"x")
    assert export_worker._export_may_proceed(db) is True
