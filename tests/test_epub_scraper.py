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
