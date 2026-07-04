"""The scrape-or-cache decision is extracted into a helper so it is testable
without HTTP: routers.chapters.get_chapter_text(chapter, db)."""
import asyncio

import pytest


@pytest.fixture()
def db_novel_chapter():
    import database
    database.init_db()
    db = database.SessionLocal()
    novel = database.Novel(title="T", rr_url="https://www.royalroad.com/fiction/999001/t")
    db.add(novel); db.commit()
    ch = database.Chapter(novel_id=novel.id, title="Ch 1", order=1,
                          rr_url="https://www.royalroad.com/fiction/999001/t/chapter/1/c1")
    db.add(ch); db.commit()
    yield db, ch
    db.query(database.Chapter).filter_by(id=ch.id).delete()
    db.query(database.Novel).filter_by(id=novel.id).delete()
    db.commit(); db.close()


def test_cached_text_skips_scraping(db_novel_chapter, monkeypatch):
    db, ch = db_novel_chapter
    ch.text = "cached body"; db.commit()
    from routers import chapters as chapters_router

    def boom(url):  # any scraper resolution means we tried to scrape
        raise AssertionError("should not scrape when text is cached")
    monkeypatch.setattr(chapters_router, "_scraper_for", boom)

    text = asyncio.run(chapters_router.get_chapter_text(ch, db))
    assert text == "cached body"


def test_scrape_populates_cache(db_novel_chapter, monkeypatch):
    db, ch = db_novel_chapter
    from routers import chapters as chapters_router

    class FakeScraper:
        async def scrape_chapter_text(self, url):
            return "fresh body"
    monkeypatch.setattr(chapters_router, "_scraper_for", lambda url: FakeScraper())

    text = asyncio.run(chapters_router.get_chapter_text(ch, db))
    assert text == "fresh body"
    db.refresh(ch)
    assert ch.text == "fresh body"
