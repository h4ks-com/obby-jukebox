"""The in-memory playlist and yt-dlp resolution. The media decode/publish loop
that consumes these lives in `publisher.py` (it owns the WebRTC senders)."""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections import deque
from dataclasses import dataclass, field

import yt_dlp


class QueueFull(Exception):
    pass


@dataclass
class Item:
    url: str
    title: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


@dataclass
class Resolved:
    media_url: str
    title: str
    is_live: bool


class Playlist:
    def __init__(self, maxlen: int = 100) -> None:
        self._items: deque[Item] = deque()
        self._current: Item | None = None
        self._max = maxlen

    def add(self, url: str, title: str = "") -> Item:
        if len(self._items) >= self._max:
            raise QueueFull(f"queue is full ({self._max})")
        item = Item(url=url, title=title)
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


def resolve(url: str, cookies: str = "") -> Resolved:
    """Resolve a page URL to a direct media URL via yt-dlp."""
    opts: dict[str, object] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "best[height<=720][ext=mp4]/best[height<=720]/best",
    }
    tmp_cookies = ""
    if cookies and os.path.exists(cookies):
        # yt-dlp rewrites the cookiefile on close; the mounted secret is
        # read-only, so work on a writable copy.
        fd, tmp_cookies = tempfile.mkstemp(suffix="-cookies.txt")
        os.close(fd)
        shutil.copyfile(cookies, tmp_cookies)
        opts["cookiefile"] = tmp_cookies
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    finally:
        if tmp_cookies:
            os.unlink(tmp_cookies)
    return Resolved(
        media_url=info["url"],
        title=info.get("title", url),
        is_live=bool(info.get("is_live")),
    )
