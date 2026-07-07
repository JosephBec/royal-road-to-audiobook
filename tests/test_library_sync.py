"""library_sync.sync_chapter_list: adds only genuinely new chapters and keeps
the novel's chapter count / refresh timestamp current."""
import pytest

import library_sync


@pytest.fixture()
def novel(monkeypatch):
    import database
    database.init_db()
    db = database.SessionLocal()
    n = database.Novel(title="Sync Novel", author="A",
                       rr_url="https://www.royalroad.com/fiction/500/sync")
    db.add(n); db.commit()
    for i in (1, 2):
        db.add(database.Chapter(
            novel_id=n.id, title=f"C{i}", order=i, rr_chapter_id=str(i),
            rr_url=f"https://www.royalroad.com/fiction/500/sync/chapter/{i}/c"))
    db.commit()
    yield db, database, n
    db.query(database.Chapter).filter_by(novel_id=n.id).delete()
    db.query(database.Novel).filter_by(id=n.id).delete()
    db.commit(); db.close()


def _ch(i):
    return {
        "title": f"C{i}", "order": i, "rr_chapter_id": str(i),
        "rr_url": f"https://www.royalroad.com/fiction/500/sync/chapter/{i}/c",
        "published_at": None,
    }


def test_adds_only_new_chapters(novel):
    db, database, n = novel
    new_count = library_sync.sync_chapter_list(db, n, [_ch(1), _ch(2), _ch(3)])
    assert new_count == 1
    assert n.total_chapters == 3
    assert n.last_refreshed is not None
    orders = [c.order for c in db.query(database.Chapter)
              .filter_by(novel_id=n.id).order_by(database.Chapter.order).all()]
    assert orders == [1, 2, 3]


def test_idempotent_second_sync_adds_nothing(novel):
    db, database, n = novel
    library_sync.sync_chapter_list(db, n, [_ch(1), _ch(2), _ch(3)])
    again = library_sync.sync_chapter_list(db, n, [_ch(1), _ch(2), _ch(3)])
    assert again == 0
    assert n.total_chapters == 3


def test_total_reflects_incoming_list(novel):
    db, database, n = novel
    # Even with no new chapters, total_chapters tracks the incoming list length.
    library_sync.sync_chapter_list(db, n, [_ch(1)])
    assert n.total_chapters == 1
