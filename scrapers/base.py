"""
Base scraper interface and shared HTTP helpers.

To add a site: create scrapers/<site>.py with a BaseScraper subclass; it is
auto-discovered at startup. See royalroad.py for a template. Each subclass
must set `name` and `url_patterns` and implement the three scrape methods.
"""

import asyncio
import html
import re
from abc import ABC, abstractmethod

import httpx
from bs4 import BeautifulSoup, Tag

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT}

# Tags a novel description is allowed to keep. Source sites format these with
# real paragraphs and links (Royal Road especially); collapsing everything to
# get_text(strip=True) was throwing both away and running the whole blurb
# together. Everything else is unwrapped (text kept, tag dropped) rather than
# deleted outright, since scraped markup is never something to trust as-is.
_DESCRIPTION_ALLOWED_TAGS = {
    "p", "br", "b", "strong", "i", "em", "ul", "ol", "li", "blockquote", "a",
}


def sanitize_description_html(tag: Tag) -> str:
    """A scraped description tag -> a safe HTML string for direct rendering.

    Keeps paragraphs and links; strips scripts/styles/attributes/everything
    else. Links get target=_blank + rel=noopener so a novel's own promo links
    don't navigate the app away.
    """
    soup = BeautifulSoup(str(tag), "lxml")
    for bad in soup(["script", "style", "head", "meta", "link", "iframe", "form"]):
        bad.decompose()

    for el in soup.find_all(True):
        if el.name not in _DESCRIPTION_ALLOWED_TAGS:
            el.unwrap()
            continue
        href = el.get("href") if el.name == "a" else None
        el.attrs = {}
        if el.name == "a":
            if href and re.match(r"^https?://", href):
                el["href"] = href
                el["target"] = "_blank"
                el["rel"] = "noopener noreferrer"
            else:
                el.unwrap()  # javascript:/relative/empty hrefs — not worth keeping

    # Collapse consecutive blank paragraphs/whitespace-only text nodes left
    # behind by unwrapping, so the sanitizer's own cleanup doesn't reintroduce
    # the "wall of blank lines" version of the same readability problem.
    for el in list(soup.find_all(True)):
        if isinstance(el, Tag) and el.name in _DESCRIPTION_ALLOWED_TAGS \
                and el.name not in ("br",) and not el.get_text(strip=True) \
                and not el.find("br"):
            el.decompose()

    out = "".join(str(c) for c in
                  (soup.body.contents if soup.body else soup.contents))
    return re.sub(r"\n{2,}", "\n", out).strip()


def text_to_description_html(text: str) -> str:
    """Plain text (blank-line-separated paragraphs, e.g. an EPUB's Dublin
    Core description) -> the same paragraph HTML shape
    sanitize_description_html produces, so the frontend can render every
    source's description the same way."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return "".join(
        f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs
    )


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
