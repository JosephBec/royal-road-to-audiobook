"""Startup repair for descriptions stored before the sanitizer kept
paragraphs and links.

Old rows hold get_text(strip=True) output — every text node glued together
("onAmazonandAudible") with links dropped. The sanitizer only runs at scrape
time, so those rows stay smashed until re-scraped; this backfill does exactly
one metadata re-scrape per stale novel.
"""
import asyncio

import pytest

import database
from database import Novel, SessionLocal
from tests.epub_fixtures import make_epub


@pytest.fixture()
def db(tmp_path):
    database.init_db()
    session = SessionLocal()
    yield session
    session.close()
    cleanup = SessionLocal()
    for n in cleanup.query(Novel).all():
        cleanup.delete(n)
    cleanup.commit()
    cleanup.close()


def _novel(db, url, description):
    novel = Novel(title=f"Book {url[-8:]}", rr_url=url, description=description)
    db.add(novel)
    db.commit()
    return novel.id


def _description(novel_id):
    db = SessionLocal()
    try:
        return db.query(Novel).filter(Novel.id == novel_id).first().description
    finally:
        db.close()


class FakeScraper:
    def __init__(self, description="<p>Fresh <a href=\"https://x.example\">link</a></p>"):
        self.description = description
        self.calls = []

    async def scrape_novel_metadata(self, url):
        self.calls.append(url)
        return {"title": "T", "author": "A", "cover_url": None,
                "description": self.description, "rr_url": url}


# ----- staleness -----

@pytest.mark.parametrize("description,stale", [
    (None, False),
    ("", False),
    ("Plain smashed text with no markup at all.", True),
    ("<p>Already sanitized.</p>", False),
    ("Leading text <a href=\"https://x\">with a link</a>", False),
])
def test_is_stale(description, stale):
    import description_backfill
    assert description_backfill.is_stale(description) is stale


# ----- backfill behaviour -----

def test_stale_description_is_replaced(db, monkeypatch):
    import description_backfill
    novel_id = _novel(db, "https://www.royalroad.com/fiction/1/a", "Smashed text")
    fake = FakeScraper()
    monkeypatch.setattr(description_backfill, "get_scraper_for_url", lambda url: fake)
    monkeypatch.setattr(description_backfill, "DELAY_SECONDS", 0)

    asyncio.run(description_backfill.backfill_stale_descriptions())

    assert _description(novel_id) == fake.description


def test_html_descriptions_are_not_rescraped(db, monkeypatch):
    import description_backfill
    _novel(db, "https://www.royalroad.com/fiction/2/b", "<p>Fine already.</p>")
    fake = FakeScraper()
    monkeypatch.setattr(description_backfill, "get_scraper_for_url", lambda url: fake)
    monkeypatch.setattr(description_backfill, "DELAY_SECONDS", 0)

    asyncio.run(description_backfill.backfill_stale_descriptions())

    assert fake.calls == [], "a repaired description must not cost a scrape"


def test_empty_scrape_result_keeps_the_old_text(db, monkeypatch):
    """A layout change upstream must not blank a description we do have."""
    import description_backfill
    novel_id = _novel(db, "https://www.royalroad.com/fiction/3/c", "Old but present")
    fake = FakeScraper(description="")
    monkeypatch.setattr(description_backfill, "get_scraper_for_url", lambda url: fake)
    monkeypatch.setattr(description_backfill, "DELAY_SECONDS", 0)

    asyncio.run(description_backfill.backfill_stale_descriptions())

    assert _description(novel_id) == "Old but present"


def test_one_failing_novel_does_not_abandon_the_rest(db, monkeypatch):
    import description_backfill

    class FailingScraper:
        async def scrape_novel_metadata(self, url):
            raise RuntimeError("site down")

    fail_id = _novel(db, "https://www.royalroad.com/fiction/4/d", "Smashed one")
    ok_id = _novel(db, "https://www.royalroad.com/fiction/5/e", "Smashed two")
    fake = FakeScraper()
    scrapers = {"https://www.royalroad.com/fiction/4/d": FailingScraper(),
                "https://www.royalroad.com/fiction/5/e": fake}
    monkeypatch.setattr(description_backfill, "get_scraper_for_url", scrapers.get)
    monkeypatch.setattr(description_backfill, "DELAY_SECONDS", 0)

    asyncio.run(description_backfill.backfill_stale_descriptions())

    assert _description(fail_id) == "Smashed one", "failed scrape leaves the row alone"
    assert _description(ok_id) == fake.description


def test_epub_descriptions_refresh_from_the_local_file(db, tmp_path, monkeypatch):
    """EPUB books go through the same path; their scraper reads the file."""
    import description_backfill
    from scrapers import epub_local

    lib = tmp_path / "EPUBs"
    lib.mkdir()
    make_epub(lib / "Desc.epub", description="First paragraph.\n\nSecond one.")
    monkeypatch.setattr(epub_local, "EPUB_DIR", lib)
    monkeypatch.setattr(description_backfill, "DELAY_SECONDS", 0)

    novel_id = _novel(db, epub_local.novel_url("Desc.epub"), "First paragraph.Second one.")
    asyncio.run(description_backfill.backfill_stale_descriptions())

    # clean_html_to_text folds the blank line to a single break, so the two
    # paragraphs come back joined by <br> — the same shape a fresh import of
    # this EPUB gets. What matters is the break is back and the format is new.
    assert _description(novel_id) == "<p>First paragraph.<br>Second one.</p>"
