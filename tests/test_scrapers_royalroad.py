"""Royal Road HTML parsing, exercised against fixture pages (no network).

These lock the parser's contract with the page structure the scraper expects.
If Royal Road changes its markup, refreshing these fixtures from a real page
and re-running is how you'd confirm what broke.
"""
import asyncio

import pytest

from scrapers.royalroad import RoyalRoadScraper


class _FakeResp:
    def __init__(self, text, url="https://www.royalroad.com/fiction/12345/x"):
        self.text = text
        self.url = url


def _patch_get(monkeypatch, scraper, text):
    async def fake_get(client, url):
        return _FakeResp(text)
    monkeypatch.setattr(scraper, "_rate_limited_get", fake_get)


FICTION_HTML = """
<html><body>
<div class="fic-header">
  <h1 class="font-white">The Stone Muse</h1>
  <h4 class="font-white"><a href="/profile/999/jane">Jane Doe</a></h4>
  <img class="thumbnail" src="https://cdn.royalroad.com/cover.jpg">
</div>
<div class="description"><div class="hidden-content"><p>An epic tale.</p></div></div>
<table id="chapters"><tbody>
  <tr data-url="/fiction/12345/x/chapter/111/one">
    <td><a href="/fiction/12345/x/chapter/111/one">Chapter 1: Beginnings</a></td>
    <td><time unixtime="1700000000">a year ago</time></td>
  </tr>
  <tr data-url="/fiction/12345/x/chapter/222/two">
    <td><a href="/fiction/12345/x/chapter/222/two">Chapter 2: Onwards</a></td>
    <td><time datetime="2024-01-02T03:04:05Z">later</time></td>
  </tr>
</tbody></table>
</body></html>
"""

CHAPTER_HTML = """
<html><body>
<div class="chapter-inner chapter-content">
  <div class="author-note-portlet"><p>Please rate my story!</p></div>
  <p>The rain had stopped.</p>
  <p>Simon reached the old library.</p>
  <div class="author-note"><p>Thanks for reading!</p></div>
</div>
</body></html>
"""


def test_scrape_novel_metadata(monkeypatch):
    scraper = RoyalRoadScraper()
    _patch_get(monkeypatch, scraper, FICTION_HTML)
    meta = asyncio.run(scraper.scrape_novel_metadata(
        "https://www.royalroad.com/fiction/12345/x"))
    assert meta["title"] == "The Stone Muse"
    assert meta["author"] == "Jane Doe"
    assert meta["cover_url"] == "https://cdn.royalroad.com/cover.jpg"
    assert "epic tale" in meta["description"]
    assert meta["rr_url"] == "https://www.royalroad.com/fiction/12345/x"


def test_invalid_url_raises():
    scraper = RoyalRoadScraper()
    with pytest.raises(ValueError):
        asyncio.run(scraper.scrape_novel_metadata("https://example.com/not-rr"))


def test_scrape_chapter_list(monkeypatch):
    scraper = RoyalRoadScraper()
    _patch_get(monkeypatch, scraper, FICTION_HTML)
    chapters = asyncio.run(scraper.scrape_chapter_list(
        "https://www.royalroad.com/fiction/12345/x"))
    assert [c["order"] for c in chapters] == [1, 2]
    assert [c["title"] for c in chapters] == ["Chapter 1: Beginnings", "Chapter 2: Onwards"]
    assert chapters[0]["rr_url"] == "https://www.royalroad.com/fiction/12345/x/chapter/111/one"
    assert chapters[0]["rr_chapter_id"] == "111"
    # unixtime and ISO datetime both parse to a tz-aware datetime
    assert chapters[0]["published_at"] is not None
    assert chapters[1]["published_at"] is not None


def test_scrape_chapter_text_strips_author_notes(monkeypatch):
    scraper = RoyalRoadScraper()
    _patch_get(monkeypatch, scraper, CHAPTER_HTML)
    text = asyncio.run(scraper.scrape_chapter_text(
        "https://www.royalroad.com/fiction/12345/x/chapter/111/one"))
    assert "The rain had stopped." in text
    assert "Simon reached the old library." in text
    assert "rate my story" not in text     # author note stripped
    assert "Thanks for reading" not in text
