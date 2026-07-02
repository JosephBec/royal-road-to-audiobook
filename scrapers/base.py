"""
Base scraper interface and shared HTTP helpers.

To add a site: create scrapers/<site>.py with a BaseScraper subclass; it is
auto-discovered at startup. See royalroad.py for a template. Each subclass
must set `name` and `url_patterns` and implement the three scrape methods.
"""

import asyncio
import re
from abc import ABC, abstractmethod

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT}


class BaseScraper(ABC):
    """One instance per site. Subclasses set `name` and `url_patterns`."""

    name: str = "base"
    url_patterns: list[re.Pattern] = []

    def __init__(self):
        self._last_request_time = 0.0
        self._rate_lock = asyncio.Lock()

    def matches(self, url: str) -> bool:
        return any(p.search(url) for p in self.url_patterns)

    async def _rate_limited_get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        """GET with 1 req/sec rate limit per scraper."""
        async with self._rate_lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_request_time
            if elapsed < 1.0:
                await asyncio.sleep(1.0 - elapsed)
            self._last_request_time = asyncio.get_event_loop().time()

        response = await client.get(url, headers=HEADERS, follow_redirects=True)
        response.raise_for_status()
        return response

    @abstractmethod
    async def scrape_novel_metadata(self, url: str) -> dict:
        """Return {title, author, cover_url, description, rr_url(canonical)}."""

    @abstractmethod
    async def scrape_chapter_list(self, novel_url: str) -> list[dict]:
        """Return [{title, rr_url, rr_chapter_id, order, published_at}]."""

    @abstractmethod
    async def scrape_chapter_text(self, chapter_url: str) -> str:
        """Return chapter plain text, paragraphs separated by blank lines."""
