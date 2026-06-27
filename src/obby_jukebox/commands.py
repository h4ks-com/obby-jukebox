"""Chat commands on the stream channel, mirroring the REST control surface.

Only messages in the stream channel are honored (never PMs), and the bot's own
messages are ignored so it can never trigger itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from obby_jukebox.player import Playlist, QueueFull

_QUEUE_PREVIEW = 5


class ChannelClient(Protocol):
    nick: str

    def privmsg(self, target: str, text: str) -> None: ...


class CommandHandler:
    def __init__(
        self,
        irc: ChannelClient,
        playlist: Playlist,
        channel: str,
        wake: Callable[[], None],
        skip: Callable[[], None],
    ) -> None:
        self.irc = irc
        self.playlist = playlist
        self.channel = channel
        self.wake = wake
        self.skip = skip

    def on_message(self, sender: str, target: str, text: str) -> None:
        if target != self.channel:
            return
        if sender.casefold() == self.irc.nick.casefold():
            return
        parts = text.strip().split(maxsplit=1)
        if not parts or not parts[0].startswith("."):
            return
        cmd = parts[0][1:].casefold()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "play":
            self._play(arg)
        elif cmd == "skip":
            self.skip()
            self._reply("skipped")
        elif cmd == "clear":
            self.playlist.clear()
            self._reply("queue cleared")
        elif cmd == "now":
            self._now()
        elif cmd == "queue":
            self._queue()
        elif cmd == "help":
            self._reply("commands: .play <url> | .skip | .clear | .now | .queue")

    def _play(self, arg: str) -> None:
        if not arg:
            self._reply("usage: .play <url>")
            return
        try:
            item = self.playlist.add(arg)
        except QueueFull as e:
            self._reply(str(e))
            return
        self.wake()
        self._reply(f"queued {item.url}")

    def _now(self) -> None:
        cur = self.playlist.now
        if cur is None:
            self._reply("nothing playing")
            return
        self._reply(f"now playing: {cur.title or cur.url}")

    def _queue(self) -> None:
        upcoming = self.playlist.upcoming()
        if not upcoming:
            self._reply("queue empty")
            return
        titles = ", ".join(i.title or i.url for i in upcoming[:_QUEUE_PREVIEW])
        extra = len(upcoming) - _QUEUE_PREVIEW
        self._reply(f"queue: {titles}" + (f" (+{extra} more)" if extra > 0 else ""))

    def _reply(self, text: str) -> None:
        self.irc.privmsg(self.channel, text)
