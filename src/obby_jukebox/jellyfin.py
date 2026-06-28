"""Minimal async Jellyfin client: search series, list their episodes in order,
and build direct-play stream URLs the media loop can hand straight to ffmpeg."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class Series:
    id: str
    name: str
    year: int | None


@dataclass
class Episode:
    id: str
    season: int
    number: int
    title: str


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _as_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _as_opt_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


class JellyfinClient:
    def __init__(
        self, base_url: str, api_key: str, client: httpx.AsyncClient | None = None
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._client = client or httpx.AsyncClient(timeout=15)

    async def search_series(self, query: str, limit: int = 5) -> list[Series]:
        items = await self._items(
            {
                "Recursive": "true",
                "IncludeItemTypes": "Series",
                "SearchTerm": query,
                "Limit": str(limit),
                "Fields": "ProductionYear",
            }
        )
        return [
            Series(
                id=_as_str(it.get("Id")),
                name=_as_str(it.get("Name")) or "?",
                year=_as_opt_int(it.get("ProductionYear")),
            )
            for it in items
        ]

    async def episodes(self, series_id: str) -> list[Episode]:
        items = await self._items(
            {
                "Recursive": "true",
                "ParentId": series_id,
                "IncludeItemTypes": "Episode",
            }
        )
        episodes = [
            Episode(
                id=_as_str(it.get("Id")),
                season=_as_int(it.get("ParentIndexNumber")),
                number=_as_int(it.get("IndexNumber")),
                title=_as_str(it.get("Name")),
            )
            for it in items
        ]
        # Sort client-side so order never depends on the server honoring SortBy.
        episodes.sort(key=lambda e: (e.season, e.number))
        return episodes

    def stream_url(self, item_id: str) -> str:
        # static=true → direct play of the original file; ffmpeg decodes it.
        return f"{self._base}/Videos/{item_id}/stream?static=true&api_key={self._key}"

    async def _items(self, params: dict[str, str]) -> list[dict[str, object]]:
        merged = {"api_key": self._key, **params}
        r = await self._client.get(self._base + "/Items", params=merged)
        r.raise_for_status()
        payload = r.json()
        items = payload.get("Items", []) if isinstance(payload, dict) else []
        return [it for it in items if isinstance(it, dict)]
