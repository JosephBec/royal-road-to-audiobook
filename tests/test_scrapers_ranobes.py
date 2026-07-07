"""Ranobes HTML/JSON parsing, exercised against fixture pages (no network)."""
import asyncio

import pytest

from scrapers.ranobes import RanobesScraper


class _FakeResp:
    def __init__(self, text, url):
        self.text = text
        self.url = url


NOVEL_HTML = """
<html><body>
<h1 class="title">Great Novel <span>by Someone</span></h1>
<a href="/authors/5-someone/">Someone</a>
<div class="poster"><img src="/uploads/cover.jpg"></div>
<div class="moreless__full">A grand adventure.</div>
</body></html>
"""

# chapter list page embeds window.__DATA__ JSON, newest-first
CHAPTERS_JSON_HTML = """
<script>window.__DATA__ = {"pages_count": 1, "chapters": [
  {"id": 20, "title": "Chapter 2", "link": "/read-chapter/20/", "date": "2024-01-02 00:00:00"},
  {"id": 10, "title": "Chapter 1", "link": "/read-chapter/10/", "date": "2024-01-01 00:00:00"}
]}</script>
"""

CHAPTER_HTML = """
<html><body>
<div id="arrticle">
  <div class="free-support">Support us on Patreon</div>
  <p>The hero awoke.</p>
  <p>Dawn broke over the hills.</p>
</div>
</body></html>
"""


def test_scrape_novel_metadata(monkeypatch):
    scraper = RanobesScraper()
    url = "https://ranobes.net/novels/777-great-novel.html"
    async def fake_get(client, u):
        return _FakeResp(NOVEL_HTML, url)
    monkeypatch.setattr(scraper, "_rate_limited_get", fake_get)
    meta = asyncio.run(scraper.scrape_novel_metadata(url))
    assert meta["title"] == "Great Novel"           # nested "by" span dropped
    assert meta["author"] == "Someone"
    assert meta["cover_url"] == "https://ranobes.net/uploads/cover.jpg"
    assert "grand adventure" in meta["description"]


def test_invalid_url_raises():
    scraper = RanobesScraper()
    with pytest.raises(ValueError):
        asyncio.run(scraper.scrape_novel_metadata("https://ranobes.net/not-a-novel"))


def test_scrape_chapter_list_orders_oldest_first(monkeypatch):
    scraper = RanobesScraper()
    async def fake_get(client, u):
        return _FakeResp(CHAPTERS_JSON_HTML, u)
    monkeypatch.setattr(scraper, "_rate_limited_get", fake_get)
    chapters = asyncio.run(scraper.scrape_chapter_list(
        "https://ranobes.net/novels/777-great-novel.html"))
    # source is newest-first; scraper reverses to reading order
    assert [c["title"] for c in chapters] == ["Chapter 1", "Chapter 2"]
    assert [c["order"] for c in chapters] == [1, 2]
    assert chapters[0]["rr_url"] == "https://ranobes.net/read-chapter/10/"
    assert chapters[0]["published_at"] is not None


def test_scrape_chapter_text_strips_support_blocks(monkeypatch):
    scraper = RanobesScraper()
    async def fake_get(client, u):
        return _FakeResp(CHAPTER_HTML, u)
    monkeypatch.setattr(scraper, "_rate_limited_get", fake_get)
    text = asyncio.run(scraper.scrape_chapter_text("https://ranobes.net/read-chapter/10/"))
    assert "The hero awoke." in text
    assert "Dawn broke over the hills." in text
    assert "Patreon" not in text     # .free-support stripped
