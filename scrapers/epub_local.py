"""
Local EPUB source: pseudo-URL helpers and file parsing.

Books in the EPUBs folder flow through the same scraper machinery as web
novels by using pseudo-URLs:

    novel:   epub://Mother%20of%20Learning.epub
    chapter: epub://Mother%20of%20Learning.epub#12   (12 = 0-based chapter index)

EPUB_DIR defaults to <repo>/EPUBs (override with NOVEL_TTS_EPUB_DIR). Access
it as `epub_local.EPUB_DIR` — a module attribute read at call time — so tests
can monkeypatch it.
"""

import asyncio
import logging
import os
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from scrapers.base import BaseScraper

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

logger = logging.getLogger(__name__)

EPUB_DIR = Path(os.environ.get(
    "NOVEL_TTS_EPUB_DIR",
    str(Path(__file__).resolve().parent.parent / "EPUBs"),
))
URL_SCHEME = "epub://"
MIN_CHAPTER_WORDS = 20  # spine items below this are front matter, not chapters


def covers_dir() -> Path:
    return EPUB_DIR / ".covers"


# ===== Pseudo-URL helpers =====

def novel_url(filename: str) -> str:
    return URL_SCHEME + quote(filename)


def chapter_url(filename: str, index: int) -> str:
    return f"{novel_url(filename)}#{index}"


def filename_from_url(url: str) -> str:
    return unquote(url[len(URL_SCHEME):].split("#", 1)[0])


def chapter_index_from_url(url: str) -> int:
    return int(url.split("#", 1)[1])


# ===== Parsing =====

@dataclass
class ParsedChapter:
    title: str
    text: str
    index: int
    word_count: int = 0

    def __post_init__(self):
        self.word_count = len(self.text.split())


@dataclass
class ParsedBook:
    title: str = "Unknown Title"
    author: str = "Unknown Author"
    description: str = ""
    chapters: list[ParsedChapter] = field(default_factory=list)
    cover: bytes | None = None
    cover_ext: str = "jpg"


def clean_html_to_text(html_content: str) -> str:
    """Strip tags, normalize whitespace, keep paragraph breaks as blank lines."""
    soup = BeautifulSoup(html_content, "lxml")
    for element in soup(["script", "style", "head", "meta", "link"]):
        element.decompose()
    text = soup.get_text(separator="\n")
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            lines.append(re.sub(r"[ \t]+", " ", stripped))
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean_chapter_title(title: str) -> str:
    """'34 34. Problem' -> '34. Problem' (some EPUBs double the number)."""
    match = re.match(r"^(\d+)\s+(\d+[\.\):\-\s])", title)
    if match:
        leading = int(match.group(1))
        second = int(re.match(r"\d+", match.group(2)).group())
        if leading == second:
            return title[match.end(1):].lstrip()
    return title


def extract_chapter_title(html_content: str, fallback_title: str) -> str:
    """First h1-h4 heading, else the fallback."""
    soup = BeautifulSoup(html_content, "lxml")
    for tag in ["h1", "h2", "h3", "h4"]:
        heading = soup.find(tag)
        if heading:
            title = heading.get_text(strip=True)
            if title and len(title) < 200:
                return clean_chapter_title(title)
    return fallback_title


def _cover_ext(media_type: str, filename: str) -> str:
    if "png" in media_type or filename.lower().endswith(".png"):
        return "png"
    if "gif" in media_type or filename.lower().endswith(".gif"):
        return "gif"
    if "webp" in media_type or filename.lower().endswith(".webp"):
        return "webp"
    return "jpg"


def extract_cover_image(book: epub.EpubBook) -> tuple[bytes | None, str]:
    """Try several strategies; EPUBs are wildly inconsistent about covers."""
    # EPUB3 cover-image property
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        try:
            if getattr(item, "properties", None) and "cover-image" in item.properties:
                return item.get_content(), _cover_ext(item.media_type, item.get_name())
        except Exception:
            pass
    # OPF <meta name="cover" content="id">
    cover_meta = book.get_metadata("OPF", "cover")
    if cover_meta:
        cover_id = cover_meta[0][1].get("content", "") if len(cover_meta[0]) > 1 else ""
        if cover_id:
            for item in book.get_items():
                if item.get_id() == cover_id:
                    return item.get_content(), _cover_ext(item.media_type, item.get_name())
    # image items named/id'd "cover"
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        if "cover" in item.get_name().lower() or "cover" in (item.get_id() or "").lower():
            return item.get_content(), _cover_ext(item.media_type, item.get_name())
    # a cover XHTML page wrapping an <img>
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        if "cover" in item.get_name().lower() or "cover" in (item.get_id() or "").lower():
            try:
                soup = BeautifulSoup(item.get_content().decode("utf-8", errors="replace"), "lxml")
                img = soup.find("img")
                if img and img.get("src"):
                    tail = img["src"].split("/")[-1]
                    for img_item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
                        if img_item.get_name().endswith(tail):
                            return img_item.get_content(), _cover_ext(img_item.media_type, img_item.get_name())
            except Exception:
                pass
    # EpubCover items (not typed ITEM_IMAGE by ebooklib)
    for item in book.get_items():
        if (item.get_id() or "").lower() == "cover" or type(item).__name__ == "EpubCover":
            try:
                content = item.get_content()
                if content and len(content) > 1000:  # real image, not markup
                    return content, _cover_ext(getattr(item, "media_type", ""), item.get_name())
            except Exception:
                pass
    return None, "jpg"


def parse_epub_file(path: Path, min_chapter_words: int = MIN_CHAPTER_WORDS) -> ParsedBook:
    """Parse an EPUB into metadata + chapters. Raises on missing/broken files
    and on books with no extractable chapters."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"EPUB file not found: {path}")

    book = epub.read_epub(str(path), options={"ignore_ncx": False})
    parsed = ParsedBook()

    title = book.get_metadata("DC", "title")
    if title:
        parsed.title = title[0][0]
    creator = book.get_metadata("DC", "creator")
    if creator:
        parsed.author = creator[0][0]
    desc = book.get_metadata("DC", "description")
    if desc:
        parsed.description = clean_html_to_text(desc[0][0])

    parsed.cover, parsed.cover_ext = extract_cover_image(book)

    documents = []
    for idref, _linear in book.spine:
        item = book.get_item_with_id(idref)
        if item is not None and item.get_type() == ebooklib.ITEM_DOCUMENT:
            documents.append(item)
    if not documents:
        # Odd/malformed EPUBs whose spine doesn't resolve to any documents:
        # fall back to manifest order rather than yielding zero chapters.
        documents = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))

    index = 0
    for item in documents:
        try:
            content = item.get_content().decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning("Failed to decode item %s: %s", item.get_name(), e)
            continue
        text = clean_html_to_text(content)
        if len(text.split()) < min_chapter_words:
            continue  # cover page, TOC, copyright, etc.
        parsed.chapters.append(ParsedChapter(
            title=extract_chapter_title(content, f"Chapter {index + 1}"),
            text=text,
            index=index,
        ))
        index += 1

    if not parsed.chapters:
        raise ValueError(f"No chapters with sufficient content found in: {path.name}")
    return parsed


class EpubScraper(BaseScraper):
    """Reads books from the local EPUBs folder instead of the web."""

    name = "epub"
    url_patterns = [re.compile(r"^epub://")]

    async def scrape_novel_metadata(self, url: str) -> dict:
        filename = filename_from_url(url)
        parsed = await asyncio.to_thread(parse_epub_file, EPUB_DIR / filename)
        return {
            "title": parsed.title,
            "author": parsed.author,
            "cover_url": None,  # set by epub_library once the novel row exists
            "description": parsed.description,
            "rr_url": novel_url(filename),
        }

    async def scrape_chapter_list(self, novel_url_: str) -> list[dict]:
        filename = filename_from_url(novel_url_)
        parsed = await asyncio.to_thread(parse_epub_file, EPUB_DIR / filename)
        return [{
            "title": ch.title,
            "rr_url": chapter_url(filename, ch.index),
            "rr_chapter_id": str(ch.index),
            "order": ch.index + 1,
            "published_at": None,
        } for ch in parsed.chapters]

    async def scrape_chapter_text(self, chapter_url_: str) -> str:
        filename = filename_from_url(chapter_url_)
        index = chapter_index_from_url(chapter_url_)
        parsed = await asyncio.to_thread(parse_epub_file, EPUB_DIR / filename)
        for ch in parsed.chapters:
            if ch.index == index:
                return ch.text
        raise ValueError(f"Chapter {index} not found in {filename}")
