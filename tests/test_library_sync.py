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


def test_new_chapters_get_collision_free_orders(novel):
    """After a stub the crawl re-indexes from 1. New chapters must be appended
    after the existing max order, never reusing an order already in the library."""
    db, database, n = novel  # stored: ch1 (order 1), ch2 (order 2)

    def _ch_url(i):
        return {"title": f"C{i}", "order": 1,  # crawl gives everything order 1 upward
                "rr_chapter_id": str(i),
                "rr_url": f"https://www.royalroad.com/fiction/500/sync/chapter/{i}/c",
                "published_at": None}

    # A post-stub crawl returns existing ch2 (now "order 1") plus new ch3 ("order 2").
    incoming = [dict(_ch_url(2), order=1), dict(_ch_url(3), order=2)]
    new_count = library_sync.sync_chapter_list(db, n, incoming)
    assert new_count == 1

    orders = [c.order for c in db.query(database.Chapter).filter_by(novel_id=n.id).all()]
    assert len(orders) == len(set(orders)), f"order collision: {orders}"
    ch3 = db.query(database.Chapter).filter_by(
        rr_url="https://www.royalroad.com/fiction/500/sync/chapter/3/c").first()
    assert ch3.order == 3  # appended after existing max (2), not the crawl's order 2


def test_stub_shorter_crawl_does_not_shrink_or_delete(novel):
    """Stubbing: a later crawl returns fewer chapters than we've stored.
    We must keep every stored chapter and never let the count drop below them."""
    db, database, n = novel  # starts with chapters 1 and 2 stored
    # Grow to 3, then simulate the author stubbing chapter 3 away at the source.
    library_sync.sync_chapter_list(db, n, [_ch(1), _ch(2), _ch(3)])
    assert n.total_chapters == 3

    removed = library_sync.sync_chapter_list(db, n, [_ch(1), _ch(2)])  # ch3 gone from source
    assert removed == 0
    assert n.total_chapters == 3          # count reflects the library, not the crawl
    stored = db.query(database.Chapter).filter_by(novel_id=n.id).count()
    assert stored == 3                    # chapter 3 is still playable
