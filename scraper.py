"""
Royal Road Scraper

Fetches novel metadata, chapter lists, and chapter text from Royal Road.
Rate-limited to 1 request/second to be polite.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

RR_BASE = "https://www.royalroad.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": USER_AGENT}

# Rate limit: 1 request per second
_last_request_time = 0.0
_rate_lock = asyncio.Lock()


async def _rate_limited_get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """Make a GET request with rate limiting (1 req/sec)."""
    global _last_request_time
    async with _rate_lock:
        now = asyncio.get_event_loop().time()
        elapsed = now - _last_request_time
        if elapsed < 1.0:
            await asyncio.sleep(1.0 - elapsed)
        _last_request_time = asyncio.get_event_loop().time()

    response = await client.get(url, headers=HEADERS, follow_redirects=True)
    response.raise_for_status()
    return response


def _parse_rr_url(url: str) -> Optional[str]:
    """
    Extract the fiction ID path from a Royal Road URL.
    Accepts: https://www.royalroad.com/fiction/12345/novel-name
    Returns: /fiction/12345/novel-name
    """
    match = re.match(r"https?://(?:www\.)?royalroad\.com(/fiction/\d+[^?\s]*)", url)
    if match:
        return match.group(1)
    return None


async def scrape_novel_metadata(url: str) -> dict:
    """
    Scrape novel metadata from a Royal Road fiction page.

    Returns dict with: title, author, cover_url, description, rr_url
    """
    fiction_path = _parse_rr_url(url)
    if not fiction_path:
        raise ValueError(f"Invalid Royal Road URL: {url}")

    canonical_url = RR_BASE + fiction_path
    logger.info("Scraping novel metadata: %s", canonical_url)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await _rate_limited_get(client, canonical_url)

    soup = BeautifulSoup(resp.text, "lxml")

    # Title
    title_tag = soup.select_one("h1.font-white")
    title = title_tag.get_text(strip=True) if title_tag else "Unknown Title"

    # Author
    author_tag = soup.select_one("h4.font-white a[href*='/profile/']")
    author = author_tag.get_text(strip=True) if author_tag else "Unknown Author"

    # Cover image
    cover_tag = soup.select_one("div.fic-header img.thumbnail")
    cover_url = cover_tag["src"] if cover_tag and cover_tag.get("src") else None

    # Description
    desc_tag = soup.select_one("div.description div.hidden-content")
    if not desc_tag:
        desc_tag = soup.select_one("div.description")
    description = desc_tag.get_text(strip=True) if desc_tag else ""

    return {
        "title": title,
        "author": author,
        "cover_url": cover_url,
        "description": description,
        "rr_url": canonical_url,
    }


async def scrape_chapter_list(novel_url: str) -> list[dict]:
    """
    Scrape the full chapter list from a Royal Road fiction page.

    Returns list of dicts with: title, rr_url, rr_chapter_id, order, published_at
    """
    fiction_path = _parse_rr_url(novel_url)
    if not fiction_path:
        raise ValueError(f"Invalid Royal Road URL: {novel_url}")

    canonical_url = RR_BASE + fiction_path
    logger.info("Scraping chapter list: %s", canonical_url)

    chapters = []
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await _rate_limited_get(client, canonical_url)

    soup = BeautifulSoup(resp.text, "lxml")

    # Chapter list is in a table or script tag with chapter data
    chapter_rows = soup.select("table#chapters tbody tr[data-url]")

    if not chapter_rows:
        # Fallback: try the chapter list table rows
        chapter_rows = soup.select("table.table tbody tr[data-url]")

    for idx, row in enumerate(chapter_rows):
        chapter_url = row.get("data-url", "")
        if chapter_url and not chapter_url.startswith("http"):
            chapter_url = RR_BASE + chapter_url

        # Title
        title_cell = row.select_one("td:first-child a")
        title = title_cell.get_text(strip=True) if title_cell else f"Chapter {idx + 1}"

        # Extract chapter ID from URL
        ch_id_match = re.search(r"/chapter/(\d+)", chapter_url)
        rr_chapter_id = ch_id_match.group(1) if ch_id_match else str(idx)

        # Published date
        time_tag = row.select_one("td time")
        published_at = None
        if time_tag and time_tag.get("unixtime"):
            try:
                ts = int(time_tag["unixtime"])
                published_at = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, TypeError):
                pass
        elif time_tag and time_tag.get("datetime"):
            try:
                published_at = datetime.fromisoformat(time_tag["datetime"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        chapters.append({
            "title": title,
            "rr_url": chapter_url,
            "rr_chapter_id": rr_chapter_id,
            "order": idx + 1,
            "published_at": published_at,
        })

    logger.info("Found %d chapters for %s", len(chapters), canonical_url)
    return chapters


async def scrape_chapter_text(chapter_url: str) -> str:
    """
    Scrape the chapter text content from a Royal Road chapter page.

    Returns clean text with paragraphs separated by newlines.
    """
    logger.info("Scraping chapter text: %s", chapter_url)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await _rate_limited_get(client, chapter_url)

    soup = BeautifulSoup(resp.text, "lxml")

    # Chapter content is in div.chapter-inner.chapter-content
    content_div = soup.select_one("div.chapter-inner.chapter-content")
    if not content_div:
        content_div = soup.select_one("div.chapter-content")
    if not content_div:
        raise ValueError(f"Could not find chapter content at: {chapter_url}")

    # Remove author notes (before and after chapter)
    for note in content_div.select("div.author-note-portlet, div.author-note"):
        note.decompose()

    # Remove any script/style tags
    for tag in content_div.select("script, style"):
        tag.decompose()

    # Extract text, preserving paragraph breaks
    paragraphs = []
    for elem in content_div.find_all(["p", "div", "br"]):
        text = elem.get_text(strip=True)
        if text:
            paragraphs.append(text)

    if not paragraphs:
        # Fallback: just get all text
        text = content_div.get_text(separator="\n", strip=True)
        return text

    return "\n\n".join(paragraphs)
