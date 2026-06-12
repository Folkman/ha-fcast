"""Ephemeral HTTP serving of generated content to FCast receivers.

Receivers fetch media themselves, so rendered message cards and camera
snapshots are stashed in memory under unguessable tokens and served from
an unauthenticated view for a short TTL.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import get_url

SERVE_URL = "/api/fcast/serve/{token}"
DEFAULT_TTL = 600


@dataclass
class _ServedItem:
    data: bytes
    content_type: str
    expires: float


class MediaStore:
    """In-memory token -> bytes store with expiry."""

    def __init__(self) -> None:
        self._items: dict[str, _ServedItem] = {}

    def add(self, data: bytes, content_type: str, ttl: float = DEFAULT_TTL) -> str:
        self._purge()
        token = secrets.token_urlsafe(24)
        self._items[token] = _ServedItem(data, content_type, time.time() + ttl)
        return token

    def get(self, token: str) -> _ServedItem | None:
        self._purge()
        return self._items.get(token)

    def _purge(self) -> None:
        now = time.time()
        for token in [t for t, item in self._items.items() if item.expires < now]:
            del self._items[token]


class FCastServeView(HomeAssistantView):
    """Serves stored items to the local network without authentication.

    Tokens are 144-bit random and short-lived; this mirrors how HA's own
    signed media paths allow LAN devices to fetch without credentials.
    """

    url = SERVE_URL
    name = "api:fcast:serve"
    requires_auth = False

    def __init__(self, store: MediaStore) -> None:
        self._store = store

    async def get(self, request: web.Request, token: str) -> web.Response:
        item = self._store.get(token)
        if item is None:
            return web.Response(status=404)
        return web.Response(
            body=item.data,
            content_type=item.content_type,
            headers={"Cache-Control": "no-store"},
        )


def build_serve_url(hass: HomeAssistant, token: str) -> str:
    """Absolute URL a LAN receiver can fetch; prefers the internal URL."""
    base = get_url(hass, prefer_external=False, allow_cloud=False)
    return f"{base}{SERVE_URL.format(token=token)}"
