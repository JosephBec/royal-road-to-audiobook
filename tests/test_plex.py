import asyncio

import httpx
import pytest

import plex


def _transport(handler):
    return httpx.MockTransport(handler)


def test_list_libraries_parses_sections():
    def handler(request):
        assert request.url.path == "/library/sections"
        assert request.url.params["X-Plex-Token"] == "tok"
        return httpx.Response(200, json={"MediaContainer": {"Directory": [
            {"key": "5", "title": "Audiobooks", "type": "artist"},
            {"key": "1", "title": "Movies", "type": "movie"},
        ]}})

    libs = asyncio.run(plex.list_libraries("http://plex:32400", "tok",
                                           transport=_transport(handler)))
    assert libs == [{"id": "5", "title": "Audiobooks", "type": "artist"},
                    {"id": "1", "title": "Movies", "type": "movie"}]


def test_trigger_refresh_hits_section_endpoint():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200)

    asyncio.run(plex.trigger_refresh("http://plex:32400/", "tok", "5",
                                     transport=_transport(handler)))
    assert calls == ["/library/sections/5/refresh"]


def test_connection_error_raises_plex_unreachable():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(plex.PlexUnreachable):
        asyncio.run(plex.trigger_refresh("http://plex:32400", "tok", "5",
                                         transport=_transport(handler)))


def test_http_error_is_not_unreachable():
    def handler(request):
        return httpx.Response(401)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(plex.list_libraries("http://plex:32400", "tok",
                                        transport=_transport(handler)))
