from collections.abc import Coroutine
from typing import NamedTuple

import httpx

from obby_jukebox.commands import CommandHandler
from obby_jukebox.fallback import FallbackShow
from obby_jukebox.jellyfin import JellyfinClient
from obby_jukebox.player import Playlist

SERIES = [{"Id": "s1", "Name": "Breaking Bad", "ProductionYear": 2008}]
EPISODES = [
    {"Id": "e1", "ParentIndexNumber": 1, "IndexNumber": 1, "Name": "Pilot"},
    {"Id": "e2", "ParentIndexNumber": 1, "IndexNumber": 2, "Name": "Cat's in the Bag"},
    {"Id": "e3", "ParentIndexNumber": 2, "IndexNumber": 1, "Name": "737"},
]


class FakeIrc:
    def __init__(self, nick: str = "jukebox") -> None:
        self.nick = nick
        self.sent: list[tuple[str, str]] = []
        self.reacted: list[tuple[str, str, str]] = []

    def privmsg(self, target: str, text: str) -> None:
        self.sent.append((target, text))

    def react(self, target: str, msgid: str, emoji: str) -> None:
        self.reacted.append((target, msgid, emoji))


def _jellyfin() -> JellyfinClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("IncludeItemTypes") == "Series":
            return httpx.Response(200, json={"Items": SERIES})
        return httpx.Response(200, json={"Items": EPISODES})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JellyfinClient("http://jf", "key", client=client)


class Harness(NamedTuple):
    handler: CommandHandler
    irc: FakeIrc
    playlist: Playlist
    woke: list[bool]
    skipped: list[bool]
    fallback: FallbackShow
    coros: list[Coroutine[object, object, None]]


def _handler(
    nick: str = "jukebox", channel: str = "$jukebox", admins: set[str] | None = None
) -> Harness:
    irc = FakeIrc(nick)
    playlist = Playlist()
    fallback = FallbackShow(_jellyfin())
    woke: list[bool] = []
    skipped: list[bool] = []
    coros: list[Coroutine[object, object, None]] = []
    handler = CommandHandler(
        irc,
        playlist,
        channel,
        lambda: woke.append(True),
        lambda: skipped.append(True),
        fallback,
        admins or set(),
        spawn=coros.append,
    )
    return Harness(handler, irc, playlist, woke, skipped, fallback, coros)


def test_play_adds_and_wakes():
    h = _handler()
    h.handler.on_message("alice", "$jukebox", ".play http://x/v")
    assert [i.url for i in h.playlist.upcoming()] == ["http://x/v"]
    assert h.woke == [True]
    assert "http://x/v" in h.irc.sent[-1][1]


def test_play_reacts_with_msgid():
    h = _handler()
    h.handler.on_message("alice", "$jukebox", ".play http://x/v", msgid="abc123")
    assert [i.url for i in h.playlist.upcoming()] == ["http://x/v"]
    assert h.woke == [True]
    assert h.irc.reacted == [("$jukebox", "abc123", "✅")]
    assert h.irc.sent == []  # the reaction is the ack; no redundant text reply


def test_skip_calls_skip():
    h = _handler()
    h.handler.on_message("alice", "$jukebox", ".skip")
    assert h.skipped == [True]


def test_clear_empties_queue():
    h = _handler()
    h.playlist.add("http://x/1")
    h.handler.on_message("alice", "$jukebox", ".clear")
    assert h.playlist.upcoming() == []


def test_now_reports_current():
    h = _handler()
    h.playlist.add("http://x/1", title="Song")
    h.playlist.take_next()
    h.handler.on_message("alice", "$jukebox", ".now")
    assert "Song" in h.irc.sent[-1][1]


def test_queue_lists_upcoming():
    h = _handler()
    h.playlist.add("http://x/1", title="A")
    h.handler.on_message("alice", "$jukebox", ".queue")
    assert "A" in h.irc.sent[-1][1]


def test_pm_is_ignored():
    h = _handler()
    h.handler.on_message("alice", "jukebox", ".play http://x/v")
    assert h.playlist.upcoming() == []
    assert h.irc.sent == []
    assert h.woke == []


def test_self_echo_is_ignored():
    h = _handler()
    h.handler.on_message("jukebox", "$jukebox", ".play http://x/v")
    assert h.playlist.upcoming() == []


def test_self_echo_uses_live_nick():
    h = _handler()
    h.irc.nick = "jukebox_"  # 433 fallback changed it after connect
    h.handler.on_message("jukebox_", "$jukebox", ".play http://x/v")
    assert h.playlist.upcoming() == []


def test_non_command_is_ignored():
    h = _handler()
    h.handler.on_message("alice", "$jukebox", "hello world")
    assert h.playlist.upcoming() == []
    assert h.irc.sent == []


def test_play_without_url_replies_usage():
    h = _handler()
    h.handler.on_message("alice", "$jukebox", ".play")
    assert h.playlist.upcoming() == []
    assert "usage" in h.irc.sent[-1][1].lower()


def test_show_requires_admin():
    h = _handler(admins={"mattf"})
    h.handler.on_message("eve", "$jukebox", ".show breaking", account="eve")
    assert h.coros == []
    assert h.irc.sent[-1][1] == "admins only"
    h.handler.on_message("eve", "$jukebox", ".show breaking", account=None)
    assert h.irc.sent[-1][1] == "admins only"


async def test_show_sets_fallback_from_season_episode():
    h = _handler(admins={"mattf"})
    h.handler.on_message("mattf", "$jukebox", ".show breaking S01E02", account="mattf")
    assert len(h.coros) == 1
    await h.coros[0]
    assert h.fallback.active
    assert h.woke == [True]  # kicks the media loop out of idle
    assert "S01E02" in h.fallback.status()
    assert "Breaking Bad" in h.irc.sent[-1][1]


async def test_show_search_lists_matches():
    h = _handler(admins={"mattf"})
    h.handler.on_message("mattf", "$jukebox", ".show search breaking", account="mattf")
    await h.coros[0]
    assert "Breaking Bad" in h.irc.sent[-1][1]
    assert not h.fallback.active  # search doesn't change the current show


async def test_show_off_clears():
    h = _handler(admins={"mattf"})
    h.handler.on_message("mattf", "$jukebox", ".show breaking", account="mattf")
    await h.coros[0]
    assert h.fallback.active
    h.handler.on_message("mattf", "$jukebox", ".show off", account="mattf")
    assert not h.fallback.active


async def test_now_reports_fallback_when_idle():
    h = _handler(admins={"mattf"})
    h.handler.on_message("mattf", "$jukebox", ".show breaking S01E01", account="mattf")
    await h.coros[0]
    h.irc.sent.clear()
    h.handler.on_message("alice", "$jukebox", ".now")
    assert "Breaking Bad" in h.irc.sent[-1][1]
