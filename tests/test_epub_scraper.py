"""Local EPUB source: pseudo-URLs and file parsing."""
import pytest

from tests.epub_fixtures import make_epub, LONG_PARA, COVER_BYTES


def test_url_roundtrip():
    from scrapers import epub_local
    name = "Mother of Learning — Book 1.epub"
    url = epub_local.novel_url(name)
    assert url.startswith("epub://")
    assert "#" not in url                      # '#' in names must be quoted
    assert epub_local.filename_from_url(url) == name
    ch = epub_local.chapter_url(name, 12)
    assert epub_local.filename_from_url(ch) == name
    assert epub_local.chapter_index_from_url(ch) == 12


def test_parse_epub_file(tmp_path):
    from scrapers import epub_local
    path = make_epub(tmp_path / "book.epub", title="My Book", author="Jane Doe",
                     description="A story.", cover=COVER_BYTES)
    parsed = epub_local.parse_epub_file(path)
    assert parsed.title == "My Book"
    assert parsed.author == "Jane Doe"
    assert parsed.description == "A story."
    assert [ch.title for ch in parsed.chapters] == ["Chapter One", "Chapter Two"]
    assert parsed.chapters[0].index == 0
    assert all(ch.word_count >= 20 for ch in parsed.chapters)
    assert LONG_PARA.split()[0] in parsed.chapters[0].text
    assert parsed.cover is not None and len(parsed.cover) > 1000


def test_parse_epub_file_uses_spine_order(tmp_path):
    """Manifest order must not dictate chapter order; spine order must win."""
    from scrapers import epub_local
    chapters = [
        ("Alpha Chapter", [LONG_PARA]),
        ("Beta Chapter", [LONG_PARA]),
        ("Gamma Chapter", [LONG_PARA]),
    ]
    path = make_epub(tmp_path / "scrambled.epub", chapters=chapters,
                      scramble_manifest=True)
    parsed = epub_local.parse_epub_file(path)
    assert [ch.title for ch in parsed.chapters] == [
        "Alpha Chapter", "Beta Chapter", "Gamma Chapter",
    ]
    assert [ch.index for ch in parsed.chapters] == [0, 1, 2]


def test_parse_missing_and_invalid(tmp_path):
    from scrapers import epub_local
    with pytest.raises(FileNotFoundError):
        epub_local.parse_epub_file(tmp_path / "nope.epub")
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"this is not a zip archive")
    with pytest.raises(Exception):
        epub_local.parse_epub_file(bad)


@pytest.fixture()
def library(tmp_path, monkeypatch):
    from scrapers import epub_local
    lib = tmp_path / "EPUBs"
    lib.mkdir()
    monkeypatch.setattr(epub_local, "EPUB_DIR", lib)
    return lib


def test_scraper_registered():
    from scrapers import get_scraper_for_url
    scraper = get_scraper_for_url("epub://Some%20Book.epub")
    assert scraper is not None and scraper.name == "epub"
    assert get_scraper_for_url("https://example.com/") is None  # no false match
    assert get_scraper_for_url("https://www.royalroad.com/fiction/1/x").name == "Royal Road"


def test_scraper_interface(library):
    import asyncio
    from scrapers import epub_local
    make_epub(library / "My Book.epub", title="My Book", author="Jane Doe")
    scraper = epub_local.EpubScraper()
    url = epub_local.novel_url("My Book.epub")

    meta = asyncio.run(scraper.scrape_novel_metadata(url))
    assert meta["title"] == "My Book"
    assert meta["author"] == "Jane Doe"
    assert meta["rr_url"] == url
    assert meta["cover_url"] is None

    chapters = asyncio.run(scraper.scrape_chapter_list(url))
    assert [c["order"] for c in chapters] == [1, 2]
    assert chapters[0]["rr_url"] == epub_local.chapter_url("My Book.epub", 0)
    assert chapters[0]["rr_chapter_id"] == "0"
    assert chapters[0]["published_at"] is None

    text = asyncio.run(scraper.scrape_chapter_text(chapters[1]["rr_url"]))
    assert LONG_PARA.split()[0] in text


def test_scraper_missing_file_and_bad_index(library):
    import asyncio
    from scrapers import epub_local
    scraper = epub_local.EpubScraper()
    with pytest.raises(FileNotFoundError):
        asyncio.run(scraper.scrape_chapter_text("epub://gone.epub#0"))
    make_epub(library / "small.epub")
    with pytest.raises(ValueError):
        asyncio.run(scraper.scrape_chapter_text(epub_local.chapter_url("small.epub", 99)))
