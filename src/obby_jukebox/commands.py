"""Chat commands on the stream channel, mirroring the REST control surface.

Only messages in the stream channel are honored (never PMs), and the bot's own
messages are ignored so it can never trigger itself. `.show` is admin-only: the
sender's logged-in IRC account (account-tag) must be in the configured allowlist.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Coroutine
from typing import Protocol

from obby_jukebox import irctext
from obby_jukebox.fallback import FallbackShow
from obby_jukebox.jellyfin import SeriesSummary
from obby_jukebox.player import Playlist, QueueFull

logger = logging.getLogger(__name__)

_QUEUE_PREVIEW = 5
_SEARCH_PREVIEW = 5
_QUEUED_REACTION = "✅"
_SXXEXX = re.compile(r"^[sS](\d{1,2})[eE](\d{1,3})$")


def _summary_title(summary: SeriesSummary) -> str:
    title = irctext.bold(summary.name)
    if summary.year:
        title += irctext.color(f" ({summary.year})", irctext.GREY)
    return title


def _format_summary(summary: SeriesSummary) -> str:
    if not summary.seasons:
        return (
            f"{_summary_title(summary)} — {irctext.color('no episodes', irctext.GREY)}"
        )
    seasons = " ".join(
        f"S{n:02d}: {summary.seasons[n]}" for n in sorted(summary.seasons)
    )
    return f"{_summary_title(summary)} — {seasons}"


def _season_lines(summary: SeriesSummary) -> list[str]:
    """One line per season so a single match reads as a watch-list."""
    if not summary.seasons:
        return [
            f"{_summary_title(summary)} — {irctext.color('no episodes', irctext.GREY)}"
        ]
    lines = [_summary_title(summary)]
    for n in sorted(summary.seasons):
        count = summary.seasons[n]
        lines.append(f"  S{n:02d}: {count} {'episode' if count == 1 else 'episodes'}")
    return lines


class ChannelClient(Protocol):
    nick: str

    def privmsg(self, target: str, text: str) -> None: ...

    def multiline_privmsg(self, target: str, lines: list[str]) -> None: ...

    def react(self, target: str, msgid: str, emoji: str) -> None: ...


def _parse_show_arg(arg: str) -> tuple[str, int, int]:
    """Split '<query> [SxxExx]' into (query, season, episode); default S01E01."""
    tokens = arg.split()
    if tokens:
        m = _SXXEXX.match(tokens[-1])
        if m:
            return " ".join(tokens[:-1]), int(m.group(1)), int(m.group(2))
    return arg, 1, 1


class CommandHandler:
    def __init__(
        self,
        irc: ChannelClient,
        playlist: Playlist,
        channel: str,
        wake: Callable[[], None],
        skip: Callable[[], None],
        reload_fallback: Callable[[], None],
        fallback: FallbackShow,
        admins: set[str],
        spawn: Callable[
            [Coroutine[object, object, None]], object
        ] = asyncio.ensure_future,
    ) -> None:
        self.irc = irc
        self.playlist = playlist
        self.channel = channel
        self.wake = wake
        self.skip = skip
        self.reload_fallback = reload_fallback
        self.fallback = fallback
        self.admins = admins
        self.spawn = spawn

    def on_message(
        self,
        sender: str,
        target: str,
        text: str,
        msgid: str | None = None,
        account: str | None = None,
    ) -> None:
        if target != self.channel:
            logger.debug("ignoring non-channel message to %s", target)
            return
        if sender.casefold() == self.irc.nick.casefold():
            return
        parts = text.strip().split(maxsplit=1)
        if not parts or not parts[0].startswith("."):
            return
        cmd = parts[0][1:].casefold()
        arg = parts[1].strip() if len(parts) > 1 else ""
        logger.info("command from %s (account=%s): .%s %s", sender, account, cmd, arg)
        if cmd == "play":
            self._play(arg, msgid)
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
        elif cmd == "show":
            self._show(account, arg)
        elif cmd == "help":
            self._help()

    def _play(self, arg: str, msgid: str | None) -> None:
        if not arg:
            self._reply("usage: .play <url>")
            return
        try:
            item = self.playlist.add(arg)
        except QueueFull as e:
            self._reply(str(e))
            return
        self.wake()
        # A ✅ reaction on the requester's message is the ack; fall back to a
        # text reply when the message carried no msgid (no message-tags).
        if msgid:
            self.irc.react(self.channel, msgid, _QUEUED_REACTION)
        else:
            self._reply(f"queued {irctext.color(item.url, irctext.TEAL)}")

    def _now(self) -> None:
        cur = self.playlist.now
        title = cur.title or cur.url if cur is not None else self.fallback.now_label()
        if title is None:
            self._reply(irctext.color("nothing playing", irctext.GREY))
            return
        self._reply(f"▶ now playing: {irctext.bold(title)}")

    def _queue(self) -> None:
        upcoming = self.playlist.upcoming()
        if not upcoming:
            if self.fallback.active:
                self._reply(self.fallback.status())
            else:
                self._reply(irctext.color("queue empty", irctext.GREY))
            return
        titles = ", ".join(i.title or i.url for i in upcoming[:_QUEUE_PREVIEW])
        extra = len(upcoming) - _QUEUE_PREVIEW
        more = irctext.color(f" (+{extra} more)", irctext.GREY) if extra > 0 else ""
        self._reply(f"{irctext.bold('queue')}: {titles}{more}")

    def _help(self) -> None:
        b = irctext.bold
        self._reply_lines(
            [
                f"{b('.play')} <url> · {b('.skip')} · {b('.clear')} · "
                f"{b('.now')} · {b('.queue')}",
                f"admin: {b('.show')} <name> [SxxExx] · "
                f"{b('.show search')} <name> · {b('.show off')}",
            ]
        )

    def _show(self, account: str | None, arg: str) -> None:
        if account is None or account.casefold() not in self.admins:
            self._reply(irctext.color("admins only (log in first)", irctext.RED))
            return
        if not self.fallback.configured:
            self._reply(
                irctext.color(
                    "fallback unavailable: no Jellyfin configured", irctext.RED
                )
            )
            return
        if not arg:
            self._reply(self.fallback.status())
            return
        if arg.casefold() == "off":
            self.fallback.clear()
            self.reload_fallback()  # stop the episode playing now, not at its end
            self._reply(f"fallback: {irctext.color('off', irctext.ORANGE)}")
            return
        if arg.casefold().startswith("search "):
            self.spawn(self._search(arg[len("search ") :].strip()))
            return
        query, season, episode = _parse_show_arg(arg)
        self.spawn(self._set_show(query, season, episode))

    async def _search(self, query: str) -> None:
        try:
            results = await self.fallback.search_detailed(query, _SEARCH_PREVIEW)
        except (OSError, ValueError) as e:
            logger.warning("jellyfin search failed: %s", e)
            self._reply(irctext.color("search failed", irctext.RED))
            return
        if not results:
            self._reply(irctext.color(f"no series matching {query!r}", irctext.GREY))
            return
        if len(results) == 1:
            self._reply_lines(_season_lines(results[0]))
        else:
            self._reply_lines([_format_summary(s) for s in results])

    async def _set_show(self, query: str, season: int, episode: int) -> None:
        try:
            status = await self.fallback.set_series(query, season, episode)
        except LookupError as e:
            self._reply(irctext.color(str(e), irctext.RED))
            return
        except (OSError, ValueError) as e:
            logger.warning("jellyfin set_series failed: %s", e)
            self._reply(irctext.color("could not load that show", irctext.RED))
            return
        # Start the new show now: wakes the loop if idle, and cuts the
        # currently-playing episode so the switch isn't deferred to its end.
        self.reload_fallback()
        self._reply(status)

    def _reply(self, text: str) -> None:
        self.irc.privmsg(self.channel, text)

    def _reply_lines(self, lines: list[str]) -> None:
        self.irc.multiline_privmsg(self.channel, lines)
