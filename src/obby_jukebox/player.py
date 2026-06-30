"""The in-memory playlist and yt-dlp resolution. The media decode/publish loop
that consumes these lives in `publisher.py` (it owns the WebRTC senders)."""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import yt_dlp
from yt_dlp.utils import DownloadError


class QueueFull(Exception):
    pass


@dataclass
class Item:
    url: str
    title: str = ""
    duration: int | None = None  # seconds; None until known (search or resolve)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


@dataclass
class Resolved:
    media_url: str
    title: str
    duration: int | None = None
    # Jellyfin sets this to seek server-side; None means a direct source that
    # ffmpeg seeks on the way in.
    seek_url: Callable[[float], str] | None = None


@dataclass
class YtResult:
    title: str
    url: str
    uploader: str = ""
    duration: int | None = None


class SearchCache:
    """Per-(channel, user) memory of the last `.yt` results so `.play <n>` can
    queue one by index without re-searching."""

    def __init__(self) -> None:
        self._by_user: dict[tuple[str, str], list[YtResult]] = {}

    def put(self, channel: str, user: str, results: list[YtResult]) -> None:
        self._by_user[channel, user] = results

    def get(self, channel: str, user: str) -> list[YtResult]:
        return self._by_user.get((channel, user), [])


class Playlist:
    def __init__(self, maxlen: int = 100) -> None:
        self._items: deque[Item] = deque()
        self._current: Item | None = None
        self._max = maxlen

    def add(self, url: str, title: str = "", duration: int | None = None) -> Item:
        if len(self._items) >= self._max:
            raise QueueFull(f"queue is full ({self._max})")
        item = Item(url=url, title=title, duration=duration)
        self._items.append(item)
        return item

    @property
    def now(self) -> Item | None:
        return self._current

    def upcoming(self) -> list[Item]:
        return list(self._items)

    def clear(self) -> None:
        self._items.clear()

    def take_next(self) -> Item | None:
        self._current = self._items.popleft() if self._items else None
        return self._current


@contextlib.contextmanager
def _cookiefile(cookies: str) -> Iterator[str]:
    """Yield a writable copy of the cookies file — yt-dlp rewrites it on close
    and the mounted secret is read-only — or "" when none is configured."""
    if not (cookies and os.path.exists(cookies)):
        yield ""
        return
    fd, tmp = tempfile.mkstemp(suffix="-cookies.txt")
    os.close(fd)
    shutil.copyfile(cookies, tmp)
    try:
        yield tmp
    finally:
        os.unlink(tmp)


def resolve(url: str, cookies: str = "") -> Resolved:
    """Resolve a page URL to a direct media URL via yt-dlp."""
    opts: dict[str, object] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "best[height<=720][ext=mp4]/best[height<=720]/best",
        # Bound network ops so a stalled fetch surfaces as a socket timeout
        # (OSError) the caller can skip, instead of wedging the player loop.
        "socket_timeout": 20,
    }
    with _cookiefile(cookies) as cookiefile:
        if cookiefile:
            opts["cookiefile"] = cookiefile
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    duration = info.get("duration")
    return Resolved(
        media_url=info["url"],
        title=info.get("title", url),
        duration=int(duration) if duration else None,
    )


def search_youtube(query: str, cookies: str = "", limit: int = 3) -> list[YtResult]:
    """Top YouTube matches for a query. extract_flat skips per-video resolution
    so the search stays fast; `.play` resolves the chosen one later."""
    opts: dict[str, object] = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "socket_timeout": 20,
    }
    # Over-fetch so dropping channel/playlist matches still leaves `limit` videos.
    with _cookiefile(cookies) as cookiefile:
        if cookiefile:
            opts["cookiefile"] = cookiefile
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{limit + 3}:{query}", download=False)
        except DownloadError as e:
            raise ValueError(str(e)) from e
    entries = info.get("entries", []) if isinstance(info, dict) else []
    results: list[YtResult] = []
    for entry in entries:
        if not isinstance(entry, dict) or not _is_video(entry):
            continue
        url = entry.get("url") or ""
        if not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={entry.get('id', '')}"
        duration = entry.get("duration")
        results.append(
            YtResult(
                title=entry.get("title") or url,
                url=url,
                uploader=entry.get("uploader") or entry.get("channel") or "",
                duration=int(duration) if duration else None,
            )
        )
        if len(results) == limit:
            break
    return results


def _is_video(entry: dict[str, object]) -> bool:
    """A single playable video, not a channel/playlist match — resolving those
    makes yt-dlp crawl the whole listing."""
    if entry.get("ie_key") in ("YoutubeTab", "YoutubePlaylist"):
        return False
    url = entry.get("url")
    return not (
        isinstance(url, str)
        and any(m in url for m in ("/channel/", "/playlist", "/@", "/user/", "/c/"))
    )
