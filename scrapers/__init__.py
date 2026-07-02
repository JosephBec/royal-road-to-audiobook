"""
Scraper registry.

Drop a new <site>.py with a BaseScraper subclass in this directory and it is
auto-discovered — no registration code needed. Broken modules are logged and
skipped so a bad scraper file can't take down the app.
"""

import importlib
import logging
import pkgutil

from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

_scrapers: list[BaseScraper] | None = None


def discover_scrapers() -> list[BaseScraper]:
    """Import every module in this package and instantiate its scrapers."""
    global _scrapers
    if _scrapers is not None:
        return _scrapers
    _scrapers = []
    pkg = importlib.import_module("scrapers")
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        if mod_info.name == "base":
            continue
        try:
            module = importlib.import_module(f"scrapers.{mod_info.name}")
        except Exception:
            logger.exception("Skipping broken scraper module: %s", mod_info.name)
            continue
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, BaseScraper) and obj is not BaseScraper:
                _scrapers.append(obj())
                logger.info("Registered scraper: %s", obj.name)
    return _scrapers


def get_scraper_for_url(url: str) -> BaseScraper | None:
    """Return the first scraper whose URL patterns match, else None."""
    return next((s for s in discover_scrapers() if s.matches(url)), None)


def supported_sites() -> list[str]:
    return [s.name for s in discover_scrapers()]
