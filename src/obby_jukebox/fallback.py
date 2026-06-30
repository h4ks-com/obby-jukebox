"""Fallback show: when the queue is idle, walk a chosen series episode by episode
(wrapping at the end) so the channel always has something on, like a TV channel."""

from __future__ import annotations

import logging

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
        self._cursor = 0
        logger.info("fallback set to movie %s", movie.name)
        return self.status()

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
        if not self._episodes:
            return None
        ep = self._episodes[self._cursor]
        url = self._jelly.stream_url(ep.id, ep.subtitle_index)
        return Resolved(media_url=url, title=self._label(ep))

    def advance(self) -> None:
        if self._episodes:
            self._cursor = (self._cursor + 1) % len(self._episodes)

    def now_label(self) -> str | None:
        if not self._episodes:
            return None
        return self._label(self._episodes[self._cursor])

    @property
    def active(self) -> bool:
        return bool(self._episodes)

    def status(self) -> str:
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
        self._cursor = 0
