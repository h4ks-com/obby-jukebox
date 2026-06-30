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
from obby_jukebox.jellyfin import Movie, SeriesSummary
from obby_jukebox.player import (
    Playlist,
    QueueFull,
    SearchCache,
    YtResult,
    search_youtube,
)

logger = logging.getLogger(__name__)

_QUEUE_PREVIEW = 5
_SEARCH_PREVIEW = 5
_YT_PREVIEW = 3
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
        label = irctext.bold(f"S{n:02d}")
        eps = irctext.color(
            f"{count} {'episode' if count == 1 else 'episodes'}", irctext.CYAN
        )
        lines.append(f"  {label} · {eps}")
    return lines


def _fmt_duration(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _format_result(index: int, result: YtResult) -> str:
    parts = [f"{irctext.bold(f'{index}.')} {irctext.bold(result.title)}"]
    if result.uploader:
        parts.append(irctext.color(result.uploader, irctext.GREY))
    if result.duration is not None:
        parts.append(irctext.color(_fmt_duration(result.duration), irctext.TEAL))
    return " · ".join(parts)


def _format_movie(movie: Movie) -> str:
    title = irctext.bold(movie.name)
    if movie.year:
        title += irctext.color(f" ({movie.year})", irctext.GREY)
    return title


def _parse_timecode(text: str) -> int | None:
    """Seconds from `SS`, `MM:SS`, or `HH:MM:SS`; a bare number is seconds."""
    parts = text.split(":")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total


class ChannelClient(Protocol):
    nick: str

    def privmsg(self, target: str, text: str) -> None: ...

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
        seek: Callable[[float], None],
        reload_fallback: Callable[[], None],
        fallback: FallbackShow,
        admins: set[str],
        search_cache: SearchCache,
        cookies: str = "",
        search_fn: Callable[[str, str, int], list[YtResult]] = search_youtube,
        spawn: Callable[
            [Coroutine[object, object, None]], object
        ] = asyncio.ensure_future,
    ) -> None:
        self.irc = irc
        self.playlist = playlist
        self.channel = channel
        self.wake = wake
        self.skip = skip
        self.seek = seek
        self.reload_fallback = reload_fallback
        self.fallback = fallback
        self.admins = admins
        self.search_cache = search_cache
        self.cookies = cookies
        self.search_fn = search_fn
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
            self._play(sender, account, arg, msgid)
        elif cmd == "yt":
            self._yt(sender, account, arg)
        elif cmd == "skip":
            self.skip()
            self._reply("skipped")
        elif cmd == "seek":
            self._do_seek(arg)
        elif cmd == "clear":
            self.playlist.clear()
            self._reply("queue cleared")
        elif cmd == "now":
            self._now()
        elif cmd == "queue":
            self._queue()
        elif cmd == "show":
            self._show(account, arg)
        elif cmd == "showsearch":
            self._showsearch(account, arg)
        elif cmd == "movie":
            self._movie(account, arg)
        elif cmd == "moviesearch":
            self._moviesearch(account, arg)
        elif cmd == "help":
            self._help()

    def _play(
        self, sender: str, account: str | None, arg: str, msgid: str | None
    ) -> None:
        results = self.search_cache.get(self.channel, (account or sender).casefold())
        if not arg:
            if not results:
                self._reply("usage: .play <url|number> — or .yt <terms> first")
                return
            self._enqueue(results[0].url, results[0].title, msgid)
            return
        if arg.isdigit():
            index = int(arg)
            if not 1 <= index <= len(results):
                self._reply(
                    irctext.color(f"no result #{index}; run .yt first", irctext.RED)
                )
                return
            chosen = results[index - 1]
            self._enqueue(chosen.url, chosen.title, msgid)
            return
        self._enqueue(arg, "", msgid)

    def _enqueue(self, url: str, title: str, msgid: str | None) -> None:
        try:
            item = self.playlist.add(url, title)
        except QueueFull as e:
            self._reply(str(e))
            return
        self.wake()
        # A ✅ reaction on the requester's message is the ack; fall back to a
        # text reply when the message carried no msgid (no message-tags).
        if msgid:
            self.irc.react(self.channel, msgid, _QUEUED_REACTION)
        else:
            self._reply(f"queued {irctext.color(item.title or item.url, irctext.TEAL)}")

    def _yt(self, sender: str, account: str | None, arg: str) -> None:
        if not arg:
            self._reply("usage: .yt <search terms>")
            return
        self.spawn(self._run_yt((account or sender).casefold(), arg))

    async def _run_yt(self, user: str, query: str) -> None:
        try:
            results = await asyncio.to_thread(
                self.search_fn, query, self.cookies, _YT_PREVIEW
            )
        except (OSError, ValueError) as e:
            logger.warning("youtube search failed: %s", e)
            self._reply(irctext.color("search failed", irctext.RED))
            return
        if not results:
            self._reply(irctext.color(f"no videos matching {query!r}", irctext.GREY))
            return
        self.search_cache.put(self.channel, user, results)
        lines = [_format_result(i, r) for i, r in enumerate(results, 1)]
        lines.append(irctext.color("→ .play <number> to queue", irctext.GREY))
        self._reply_lines(lines)

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
                f"{b('.yt')} <terms> · {b('.play')} <url|#> · {b('.seek')} <t> · "
                f"{b('.skip')} · {b('.clear')} · {b('.now')} · {b('.queue')}",
                f"admin: {b('.show')} <name> [SxxExx] · {b('.showsearch')} <name> · "
                f"{b('.movie')} <name> · {b('.moviesearch')} <name> · {b('.show off')}",
            ]
        )

    def _require_fallback(self, account: str | None) -> bool:
        if account is None or account.casefold() not in self.admins:
            self._reply(irctext.color("admins only (log in first)", irctext.RED))
            return False
        if not self.fallback.configured:
            self._reply(
                irctext.color(
                    "fallback unavailable: no Jellyfin configured", irctext.RED
                )
            )
            return False
        return True

    def _show(self, account: str | None, arg: str) -> None:
        if not self._require_fallback(account):
            return
        if not arg:
            self._reply(self.fallback.status())
            return
        if arg.casefold() == "off":
            self.fallback.clear()
            self.reload_fallback()  # stop the episode playing now, not at its end
            self._reply(f"fallback: {irctext.color('off', irctext.ORANGE)}")
            return
        query, season, episode = _parse_show_arg(arg)
        self.spawn(self._set_show(query, season, episode))

    def _showsearch(self, account: str | None, arg: str) -> None:
        if not self._require_fallback(account):
            return
        if not arg:
            self._reply("usage: .showsearch <name>")
            return
        self.spawn(self._search(arg))

    def _movie(self, account: str | None, arg: str) -> None:
        if not self._require_fallback(account):
            return
        if not arg:
            self._reply("usage: .movie <name>")
            return
        self.spawn(self._set_movie(arg))

    async def _set_movie(self, query: str) -> None:
        try:
            status = await self.fallback.set_movie(query)
        except LookupError as e:
            self._reply(irctext.color(str(e), irctext.RED))
            return
        except (OSError, ValueError) as e:
            logger.warning("jellyfin set_movie failed: %s", e)
            self._reply(irctext.color("could not load that movie", irctext.RED))
            return
        self.reload_fallback()
        self._reply(status)

    def _moviesearch(self, account: str | None, arg: str) -> None:
        if not self._require_fallback(account):
            return
        if not arg:
            self._reply("usage: .moviesearch <name>")
            return
        self.spawn(self._run_moviesearch(arg))

    async def _run_moviesearch(self, query: str) -> None:
        try:
            movies = await self.fallback.search_movies(query, _SEARCH_PREVIEW)
        except (OSError, ValueError) as e:
            logger.warning("jellyfin movie search failed: %s", e)
            self._reply(irctext.color("search failed", irctext.RED))
            return
        if not movies:
            self._reply(irctext.color(f"no movies matching {query!r}", irctext.GREY))
            return
        self._reply_lines([_format_movie(m) for m in movies])

    def _do_seek(self, arg: str) -> None:
        seconds = _parse_timecode(arg)
        if seconds is None:
            self._reply("usage: .seek <sec | mm:ss | hh:mm:ss>")
            return
        self.seek(float(seconds))
        self._reply(f"seek → {irctext.bold(_fmt_duration(seconds))}")

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
        for line in lines:
            self.irc.privmsg(self.channel, line)
