"""Fallback show: when the queue is idle, walk a chosen series episode by episode
(wrapping at the end) so the channel always has something on, like a TV channel."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from urllib.parse import urlsplit

from obby_jukebox.jellyfin import Episode, JellyfinClient, Movie, SeriesSummary
from obby_jukebox.player import Resolved

logger = logging.getLogger(__name__)


class FallbackShow:
    def __init__(self, jelly: JellyfinClient) -> None:
        self._jelly = jelly
        self._episodes: list[Episode] = []
        self._cursor = 0
        self._series = ""
        self._is_movie = False
        self._radio_url = ""

    @property
    def configured(self) -> bool:
        return self._jelly.configured

    async def search_detailed(self, query: str, limit: int = 5) -> list[SeriesSummary]:
        results = await self._jelly.search_series(query, limit)
        summaries: list[SeriesSummary] = []
        for series in results:
            counts = await self._jelly.season_episode_counts(series.id)
            summaries.append(SeriesSummary(series.name, series.year, counts))
        return summaries

    async def search_movies(self, query: str, limit: int = 5) -> list[Movie]:
        return await self._jelly.search_movies(query, limit)

    async def set_series(self, query: str, season: int = 1, episode: int = 1) -> str:
        results = await self._jelly.search_series(query)
        if not results:
            raise LookupError(f"no series matching {query!r}")
        series = results[0]
        eps = await self._jelly.episodes(series.id)
        if not eps:
            raise LookupError(f"{series.name} has no episodes")
        self._episodes = eps
        self._series = series.name
        self._is_movie = False
        self._radio_url = ""
        self._cursor = self._index_of(season, episode)
        logger.info(
            "fallback set to %s starting at S%02dE%02d", series.name, season, episode
        )
        return self.status()

    async def set_movie(self, query: str) -> str:
        movies = await self._jelly.search_movies(query)
        if not movies:
            raise LookupError(f"no movie matching {query!r}")
        movie = movies[0]
        self._episodes = [
            Episode(
                id=movie.id,
                season=0,
                number=0,
                title="",
                subtitle_index=movie.subtitle_index,
            )
        ]
        self._series = f"{movie.name} ({movie.year})" if movie.year else movie.name
        self._is_movie = True
        self._radio_url = ""
        self._cursor = 0
        logger.info("fallback set to movie %s", movie.name)
        return self.status()

    def set_radio(self, url: str) -> str:
        """Play a live radio stream when idle. It has no video, so the loop shows
        a visualizer; it never ends, so the cursor/advance machinery is unused."""
        self._episodes = []
        self._series = ""
        self._is_movie = False
        self._cursor = 0
        self._radio_url = url
        logger.info("fallback set to radio %s", url)
        return self.status()

    @property
    def is_radio(self) -> bool:
        return bool(self._radio_url)

    def _radio_label(self) -> str:
        host = urlsplit(self._radio_url).hostname
        return f"📻 {host}" if host else "📻 radio"

    def _index_of(self, season: int, episode: int) -> int:
        for i, ep in enumerate(self._episodes):
            if (ep.season, ep.number) >= (season, episode):
                return i
        return 0

    def _label(self, ep: Episode) -> str:
        if self._is_movie:
            return self._series
        label = f"{self._series} S{ep.season:02d}E{ep.number:02d}"
        return f"{label} — {ep.title}" if ep.title else label

    def peek(self) -> Resolved | None:
        if self._radio_url:
            return Resolved(self._radio_url, self._radio_label(), live=True)
        if not self._episodes:
            return None
        ep = self._episodes[self._cursor]
        # A fresh session id per play forces its own transcode; without one
        # Jellyfin can hand back a running transcode at the wrong resolution.
        url = self._jelly.stream_url(
            ep.id, ep.subtitle_index, play_session_id=uuid.uuid4().hex
        )
        return Resolved(url, self._label(ep), seek_url=self._seek_url_for(ep))

    def _seek_url_for(self, ep: Episode) -> Callable[[float], str]:
        def build(offset: float) -> str:
            return self._jelly.stream_url(
                ep.id,
                ep.subtitle_index,
                start_seconds=offset,
                play_session_id=uuid.uuid4().hex,
            )

        return build

    def advance(self) -> None:
        if self._episodes:
            self._cursor = (self._cursor + 1) % len(self._episodes)

    def now_label(self) -> str | None:
        if self._radio_url:
            return self._radio_label()
        if not self._episodes:
            return None
        return self._label(self._episodes[self._cursor])

    @property
    def active(self) -> bool:
        return bool(self._episodes) or bool(self._radio_url)

    def status(self) -> str:
        if self._radio_url:
            return f"radio: {self._radio_label()}"
        if not self._episodes:
            return "fallback: off"
        if self._is_movie:
            return f"fallback: {self._series} (movie)"
        ep = self._episodes[self._cursor]
        return f"fallback: {self._series} (next S{ep.season:02d}E{ep.number:02d})"

    def clear(self) -> None:
        self._episodes = []
        self._series = ""
        self._is_movie = False
        self._radio_url = ""
        self._cursor = 0
