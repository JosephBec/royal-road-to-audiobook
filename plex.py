"""Minimal Plex Media Server client: list libraries, trigger section refresh.

Section-level refresh only (no path parameter): Plex runs in Docker, so the
container's filesystem paths differ from this machine's Windows paths.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

PLEX_UNREACHABLE_MSG = (
    "Plex is unreachable (is Docker running?) — "
    "the audiobook will appear after the next library scan."
)


class PlexUnreachable(Exception):
    """Plex did not answer at the network level (Docker engine down, wrong host)."""


async def _get(url: str, token: str, path: str, *, transport=None) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport) as client:
            resp = await client.get(
                f"{url.rstrip('/')}{path}",
                params={"X-Plex-Token": token},
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp
    except httpx.TransportError as e:
        # ConnectError, timeouts, DNS failures — the server never answered.
        raise PlexUnreachable(PLEX_UNREACHABLE_MSG) from e


async def list_libraries(url: str, token: str, *, transport=None) -> list[dict]:
    resp = await _get(url, token, "/library/sections", transport=transport)
    dirs = resp.json().get("MediaContainer", {}).get("Directory", [])
    return [{"id": str(d.get("key")), "title": d.get("title"), "type": d.get("type")}
            for d in dirs]


async def trigger_refresh(url: str, token: str, section_id: str, *, transport=None) -> None:
    await _get(url, token, f"/library/sections/{section_id}/refresh", transport=transport)
    logger.info("Plex section %s refresh triggered", section_id)
