"""Minimal async Jellyfin client: search series, list their episodes in order,
and build direct-play stream URLs the media loop can hand straight to ffmpeg."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# h264_vaapi (and other VBR HW encoders) refuse to open without an explicit
# target bitrate ("Bitrate must be set for VBR RC mode"), which fails the burn-in
# transcode on HEVC sources. Pass one so hardware transcode works; 8 Mbps is
# ample for the 720p the bot re-encodes to.
_BURN_VIDEO_BITRATE = 8_000_000


@dataclass
class Series:
    id: str
    name: str
    year: int | None


@dataclass
class SeriesSummary:
    name: str
    year: int | None
    seasons: dict[int, int]  # season number → episode count


@dataclass
class Movie:
    id: str
    name: str
    year: int | None
    subtitle_index: int | None = None


@dataclass
class Episode:
    id: str
    season: int
    number: int
    title: str
    subtitle_index: int | None = None  # English subtitle stream to burn in, if any


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _as_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _as_opt_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _english_subtitle_index(media_streams: object) -> int | None:
    if not isinstance(media_streams, list):
        return None
    subs = [
        s
        for s in media_streams
        if isinstance(s, dict)
        and s.get("Type") == "Subtitle"
        and _as_str(s.get("Language")).lower() in ("eng", "en")
    ]
    # Prefer a full (non-forced), default track: forced subs only cover foreign
    # dialogue and would leave most of the show untitled.
    subs.sort(key=lambda s: (bool(s.get("IsForced")), not bool(s.get("IsDefault"))))
    return _as_opt_int(subs[0].get("Index")) if subs else None


class JellyfinClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        burn_subtitles: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._burn_subtitles = burn_subtitles
        self._client = client or httpx.AsyncClient(timeout=15)

    @property
    def configured(self) -> bool:
        return bool(self._key)

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

    async def search_movies(self, query: str, limit: int = 5) -> list[Movie]:
        fields = (
            "ProductionYear,MediaStreams" if self._burn_subtitles else "ProductionYear"
        )
        items = await self._items(
            {
                "Recursive": "true",
                "IncludeItemTypes": "Movie",
                "SearchTerm": query,
                "Limit": str(limit),
                "Fields": fields,
            }
        )
        return [
            Movie(
                id=_as_str(it.get("Id")),
                name=_as_str(it.get("Name")) or "?",
                year=_as_opt_int(it.get("ProductionYear")),
                subtitle_index=(
                    _english_subtitle_index(it.get("MediaStreams"))
                    if self._burn_subtitles
                    else None
                ),
            )
            for it in items
        ]

    async def episodes(self, series_id: str) -> list[Episode]:
        params = {
            "Recursive": "true",
            "ParentId": series_id,
            "IncludeItemTypes": "Episode",
        }
        if self._burn_subtitles:
            params["Fields"] = "MediaStreams"
        items = await self._items(params)
        episodes = [
            Episode(
                id=_as_str(it.get("Id")),
                season=_as_int(it.get("ParentIndexNumber")),
                number=_as_int(it.get("IndexNumber")),
                title=_as_str(it.get("Name")),
                subtitle_index=(
                    _english_subtitle_index(it.get("MediaStreams"))
                    if self._burn_subtitles
                    else None
                ),
            )
            for it in items
        ]
        # Sort client-side so order never depends on the server honoring SortBy.
        episodes.sort(key=lambda e: (e.season, e.number))
        return episodes

    async def season_episode_counts(self, series_id: str) -> dict[int, int]:
        items = await self._items(
            {
                "Recursive": "true",
                "ParentId": series_id,
                "IncludeItemTypes": "Episode",
            }
        )
        counts: dict[int, int] = {}
        for it in items:
            season = _as_int(it.get("ParentIndexNumber"))
            counts[season] = counts.get(season, 0) + 1
        return counts

    def stream_url(
        self,
        item_id: str,
        subtitle_index: int | None = None,
        start_seconds: float = 0.0,
        play_session_id: str = "",
    ) -> str:
        if subtitle_index is None and start_seconds <= 0:
            # static=true → direct play of the original file; ffmpeg decodes it.
            return (
                f"{self._base}/Videos/{item_id}/stream?static=true&api_key={self._key}"
            )
        # A transcode lets the server burn subtitles and start at StartTimeTicks
        # (100ns units); a fresh PlaySessionId per seek stops it reusing the
        # running transcode at its current position. VideoBitrate is required for
        # VAAPI encoders to open.
        url = (
            f"{self._base}/Videos/{item_id}/stream.mkv?api_key={self._key}&Static=false"
        )
        if subtitle_index is not None:
            url += f"&SubtitleStreamIndex={subtitle_index}&SubtitleMethod=Encode"
        url += f"&VideoCodec=h264&AudioCodec=aac&VideoBitrate={_BURN_VIDEO_BITRATE}"
        if start_seconds > 0:
            url += f"&StartTimeTicks={int(start_seconds * 10_000_000)}"
        if play_session_id:
            url += f"&PlaySessionId={play_session_id}"
        return url

    async def _items(self, params: dict[str, str]) -> list[dict[str, object]]:
        merged = {"api_key": self._key, **params}
        r = await self._client.get(self._base + "/Items", params=merged)
        r.raise_for_status()
        payload = r.json()
        items = payload.get("Items", []) if isinstance(payload, dict) else []
        return [it for it in items if isinstance(it, dict)]
