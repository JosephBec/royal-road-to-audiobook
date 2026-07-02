"""
Ranobes scraper (ranobes.net, formerly ranobes.top).

Novel pages:   https://ranobes.net/novels/<id>-<slug>.html
Chapter lists: https://ranobes.net/chapters/<id>/ (paginated, newest first,
               data embedded as window.__DATA__ JSON)
Chapter text:  #arrticle on the chapter page
"""

import html
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

BASE = "https://ranobes.net"


class RanobesScraper(BaseScraper):
    name = "Ranobes"
    url_patterns = [
        re.compile(r"https?://(?:www\.)?ranobes\.(?:net|top|com)/novels/\d+"),
    ]

    @staticmethod
    def _novel_id(url: str) -> str:
        match = re.search(r"/novels/(\d+)", url)
        if not match:
            raise ValueError(f"Invalid Ranobes URL: {url}")
        return match.group(1)

    async def scrape_novel_metadata(self, url: str) -> dict:
        self._novel_id(url)  # validate early
        logger.info("Scraping Ranobes novel metadata: %s", url)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await self._rate_limited_get(client, url)

        soup = BeautifulSoup(resp.text, "lxml")

        title_tag = soup.select_one("h1.title")
        title = "Unknown Title"
        if title_tag:
            # h1 nests "by Author" and status spans — drop them
            for child in title_tag.find_all(["span", "small"]):
                child.decompose()
            title = title_tag.get_text(strip=True) or "Unknown Title"

        author_tag = soup.select_one("a[href*='/authors/']")
        author = author_tag.get_text(strip=True) if author_tag else "Unknown Author"

        cover_tag = soup.select_one(".poster img") or soup.select_one(".r-fullstory-poster img")
        cover_url = None
        if cover_tag and cover_tag.get("src"):
            cover_url = urljoin(BASE, cover_tag["src"])

        desc_tag = soup.select_one(".moreless__full") or soup.select_one(".cont-in .showcont-h")
        description = desc_tag.get_text(strip=True) if desc_tag else ""

        return {
            "title": title,
            "author": author,
            "cover_url": cover_url,
            "description": description,
            "rr_url": str(resp.url),  # canonical source URL after redirects
        }

    async def scrape_chapter_list(self, novel_url: str) -> list[dict]:
        novel_id = self._novel_id(novel_url)
        logger.info("Scraping Ranobes chapter list for novel %s", novel_id)

        newest_first: list[dict] = []
        async with httpx.AsyncClient(timeout=30) as client:
            page = 1
            while True:
                page_url = (
                    f"{BASE}/chapters/{novel_id}/"
                    if page == 1
                    else f"{BASE}/chapters/{novel_id}/page/{page}/"
                )
                resp = await self._rate_limited_get(client, page_url)
                match = re.search(r"window\.__DATA__\s*=\s*(\{.*?\})\s*<", resp.text, re.DOTALL)
                if not match:
                    raise ValueError(f"Could not find chapter data at: {page_url}")
                data = json.loads(match.group(1))
                newest_first.extend(data.get("chapters", []))
                pages_count = int(data.get("pages_count") or 1)
                if page >= pages_count:
                    break
                page += 1

        chapters = []
        for idx, ch in enumerate(reversed(newest_first)):
            link = ch.get("link", "")
            if link and not link.startswith("http"):
                link = urljoin(BASE, link)

            published_at = None
            if ch.get("date"):
                try:
                    published_at = datetime.strptime(
                        ch["date"], "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    pass

            chapters.append({
                "title": html.unescape(ch.get("title") or f"Chapter {idx + 1}"),
                "rr_url": link,
                "rr_chapter_id": str(ch.get("id") or idx),
                "order": idx + 1,
                "published_at": published_at,
            })

        logger.info("Found %d Ranobes chapters for novel %s", len(chapters), novel_id)
        return chapters

    async def scrape_chapter_text(self, chapter_url: str) -> str:
        logger.info("Scraping Ranobes chapter text: %s", chapter_url)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await self._rate_limited_get(client, chapter_url)

        soup = BeautifulSoup(resp.text, "lxml")
        content = soup.select_one("#arrticle")
        if not content:
            raise ValueError(f"Could not find chapter content at: {chapter_url}")

        for tag in content.select("script, style, .free-support, .splitnewsnavigation"):
            tag.decompose()

        text = content.get_text(separator="\n", strip=True)
        paragraphs = [line.strip() for line in text.split("\n") if line.strip()]
        return "\n\n".join(paragraphs)
