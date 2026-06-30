from collections.abc import Callable, Coroutine
from typing import NamedTuple

import httpx

from obby_jukebox.commands import CommandHandler
from obby_jukebox.fallback import FallbackShow
from obby_jukebox.jellyfin import JellyfinClient
from obby_jukebox.player import Playlist, SearchCache, YtResult

SERIES = [{"Id": "s1", "Name": "Breaking Bad", "ProductionYear": 2008}]
MOVIES = [{"Id": "m1", "Name": "Inception", "ProductionYear": 2010}]
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
        itype = request.url.params.get("IncludeItemTypes")
        if itype == "Series":
            return httpx.Response(200, json={"Items": SERIES})
        if itype == "Movie":
            return httpx.Response(200, json={"Items": MOVIES})
        return httpx.Response(200, json={"Items": EPISODES})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JellyfinClient("http://jf", "key", client=client)


class Harness(NamedTuple):
    handler: CommandHandler
    irc: FakeIrc
    playlist: Playlist
    woke: list[bool]
    skipped: list[bool]
    reloaded: list[bool]
    fallback: FallbackShow
    coros: list[Coroutine[object, object, None]]
    search_cache: SearchCache
    seeked: list[float]


def _handler(
    nick: str = "jukebox",
    channel: str = "$jukebox",
    admins: set[str] | None = None,
    yt_results: list[YtResult] | None = None,
    position: Callable[[], float | None] = lambda: None,
) -> Harness:
    irc = FakeIrc(nick)
    playlist = Playlist()
    fallback = FallbackShow(_jellyfin())
    cache = SearchCache()
    woke: list[bool] = []
    skipped: list[bool] = []
    reloaded: list[bool] = []
    seeked: list[float] = []
    coros: list[Coroutine[object, object, None]] = []

    def fake_search(query: str, cookies: str, limit: int) -> list[YtResult]:
        return yt_results or []

    handler = CommandHandler(
        irc,
        playlist,
        channel,
        lambda: woke.append(True),
        lambda: skipped.append(True),
        seeked.append,
        lambda: reloaded.append(True),
        fallback,
        admins or set(),
        cache,
        search_fn=fake_search,
        spawn=coros.append,
        position=position,
    )
    return Harness(
        handler, irc, playlist, woke, skipped, reloaded, fallback, coros, cache, seeked
    )


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
    assert "admins only" in h.irc.sent[-1][1]
    # An unauthenticated sender (no account-tag) is never an admin, even if their
    # nick happens to match the allowlist.
    h.handler.on_message("mattf", "$jukebox", ".show breaking", account=None)
    assert h.coros == []
    assert "admins only" in h.irc.sent[-1][1]


async def test_show_sets_fallback_from_season_episode():
    h = _handler(admins={"mattf"})
    h.handler.on_message("mattf", "$jukebox", ".show breaking S01E02", account="mattf")
    assert len(h.coros) == 1
    await h.coros[0]
    assert h.fallback.active
    assert h.reloaded == [True]  # starts the show now (idle or mid-episode)
    assert "S01E02" in h.fallback.status()
    assert "Breaking Bad" in h.irc.sent[-1][1]


async def test_showsearch_lists_matches():
    h = _handler(admins={"mattf"})
    h.handler.on_message("mattf", "$jukebox", ".showsearch breaking", account="mattf")
    await h.coros[0]
    texts = [text for _, text in h.irc.sent]
    # A single match breaks out one line per season as a watch-list.
    assert any("Breaking Bad" in t for t in texts)
    assert any("S01" in t and "2 episodes" in t for t in texts)
    assert any("S02" in t and "1 episode" in t for t in texts)
    assert not h.fallback.active  # search doesn't change the current show


async def test_yt_lists_results_and_caches():
    results = [
        YtResult("First Vid", "http://y/1", "Chan A", 65),
        YtResult("Second Vid", "http://y/2", "Chan B", 3725),
    ]
    h = _handler(yt_results=results)
    h.handler.on_message("alice", "$jukebox", ".yt cats", account="alice")
    await h.coros[0]
    texts = [text for _, text in h.irc.sent]
    assert any("First Vid" in t for t in texts)
    assert any("1:05" in t for t in texts)  # mm:ss
    assert any("1:02:05" in t for t in texts)  # h:mm:ss
    assert h.search_cache.get("$jukebox", "alice") == results


def test_play_by_index_queues_cached_result():
    h = _handler()
    h.search_cache.put(
        "$jukebox", "alice", [YtResult("A", "http://y/a"), YtResult("B", "http://y/b")]
    )
    h.handler.on_message("alice", "$jukebox", ".play 2", account="alice")
    assert [i.url for i in h.playlist.upcoming()] == ["http://y/b"]
    assert h.woke == [True]


def test_play_no_arg_uses_top_cached_result():
    h = _handler()
    h.search_cache.put("$jukebox", "alice", [YtResult("A", "http://y/a")])
    h.handler.on_message("alice", "$jukebox", ".play", account="alice")
    assert [i.url for i in h.playlist.upcoming()] == ["http://y/a"]


def test_play_index_out_of_range_warns():
    h = _handler()
    h.search_cache.put("$jukebox", "alice", [YtResult("A", "http://y/a")])
    h.handler.on_message("alice", "$jukebox", ".play 9", account="alice")
    assert h.playlist.upcoming() == []
    assert "no result #9" in h.irc.sent[-1][1]


def test_show_unavailable_without_jellyfin():
    irc = FakeIrc()
    fallback = FallbackShow(JellyfinClient("http://jf", ""))  # no key → unconfigured
    coros: list[Coroutine[object, object, None]] = []
    handler = CommandHandler(
        irc,
        Playlist(),
        "$jukebox",
        lambda: None,
        lambda: None,
        lambda s: None,
        lambda: None,
        fallback,
        {"mattf"},
        SearchCache(),
        spawn=coros.append,
    )
    handler.on_message("mattf", "$jukebox", ".show breaking", account="mattf")
    assert coros == []  # never reaches Jellyfin
    assert "Jellyfin" in irc.sent[-1][1]


async def test_show_off_clears():
    h = _handler(admins={"mattf"})
    h.handler.on_message("mattf", "$jukebox", ".show breaking", account="mattf")
    await h.coros[0]
    assert h.fallback.active
    before = len(h.reloaded)
    h.handler.on_message("mattf", "$jukebox", ".show off", account="mattf")
    assert not h.fallback.active
    assert len(h.reloaded) == before + 1  # cuts the episode now, not at its end


async def test_now_reports_fallback_when_idle():
    h = _handler(admins={"mattf"})
    h.handler.on_message("mattf", "$jukebox", ".show breaking S01E01", account="mattf")
    await h.coros[0]
    h.irc.sent.clear()
    h.handler.on_message("alice", "$jukebox", ".now")
    assert "Breaking Bad" in h.irc.sent[-1][1]


def test_seek_accepts_seconds_and_timecodes():
    h = _handler()
    h.playlist.add("http://x/1")  # current item, unknown duration → no upper bound
    h.playlist.take_next()
    h.handler.on_message("alice", "$jukebox", ".seek 90")
    h.handler.on_message("alice", "$jukebox", ".seek 1:30")
    h.handler.on_message("alice", "$jukebox", ".seek 1:00:00")
    assert h.seeked == [90.0, 90.0, 3600.0]


def test_seek_rejects_garbage():
    h = _handler()
    h.handler.on_message("alice", "$jukebox", ".seek soon")
    assert h.seeked == []
    assert "usage" in h.irc.sent[-1][1].lower()


def test_seek_with_nothing_playing_warns():
    h = _handler()
    h.handler.on_message("alice", "$jukebox", ".seek 30")
    assert h.seeked == []
    assert "nothing playing" in h.irc.sent[-1][1]


def test_seek_beyond_duration_is_rejected_with_length():
    h = _handler()
    h.playlist.add("http://x/1", title="Song", duration=200)
    h.playlist.take_next()
    h.handler.on_message("alice", "$jukebox", ".seek 5:00")  # 300 >= 200
    assert h.seeked == []
    reply = h.irc.sent[-1][1]
    assert "can't seek" in reply and "3:20" in reply  # surfaces the real length


def test_seek_within_duration_passes_through():
    h = _handler()
    h.playlist.add("http://x/1", title="Song", duration=200)
    h.playlist.take_next()
    h.handler.on_message("alice", "$jukebox", ".seek 1:30")
    assert h.seeked == [90.0]


def test_now_shows_elapsed_over_total():
    h = _handler(position=lambda: 23.0)
    h.playlist.add("http://x/1", title="Song", duration=215)
    h.playlist.take_next()
    h.handler.on_message("alice", "$jukebox", ".now")
    assert "0:23 / 3:35" in h.irc.sent[-1][1]


def test_queue_shows_each_duration():
    h = _handler()
    h.playlist.add("http://x/1", title="A", duration=65)
    h.handler.on_message("alice", "$jukebox", ".queue")
    assert "1:05" in h.irc.sent[-1][1]


def test_announce_now_posts_current_unprompted():
    h = _handler(position=lambda: 5.0)
    h.playlist.add("http://x/1", title="Nightcall", duration=100)
    h.playlist.take_next()
    h.handler.announce_now()
    assert "now playing" in h.irc.sent[-1][1] and "Nightcall" in h.irc.sent[-1][1]


def test_play_by_index_carries_duration():
    h = _handler()
    h.search_cache.put("$jukebox", "alice", [YtResult("A", "http://y/a", "Chan", 142)])
    h.handler.on_message("alice", "$jukebox", ".play 1", account="alice")
    assert h.playlist.upcoming()[0].duration == 142


async def test_movie_sets_fallback_without_episode_label():
    h = _handler(admins={"mattf"})
    h.handler.on_message("mattf", "$jukebox", ".movie inception", account="mattf")
    await h.coros[0]
    assert h.fallback.active
    assert "Inception" in h.fallback.status()
    assert h.reloaded == [True]
    assert "S0" not in (h.fallback.now_label() or "")  # a movie carries no SxxExx


async def test_moviesearch_lists_matches():
    h = _handler(admins={"mattf"})
    h.handler.on_message("mattf", "$jukebox", ".moviesearch inception", account="mattf")
    await h.coros[0]
    assert "Inception" in h.irc.sent[-1][1]


def test_moviesearch_requires_admin():
    h = _handler(admins={"mattf"})
    h.handler.on_message("eve", "$jukebox", ".moviesearch inception", account="eve")
    assert h.coros == []
    assert "admins only" in h.irc.sent[-1][1]
