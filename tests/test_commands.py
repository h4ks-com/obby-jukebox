from obby_jukebox.commands import CommandHandler
from obby_jukebox.player import Playlist


class FakeIrc:
    def __init__(self, nick: str = "jukebox") -> None:
        self.nick = nick
        self.sent: list[tuple[str, str]] = []
        self.reacted: list[tuple[str, str, str]] = []

    def privmsg(self, target: str, text: str) -> None:
        self.sent.append((target, text))

    def react(self, target: str, msgid: str, emoji: str) -> None:
        self.reacted.append((target, msgid, emoji))


def _handler(nick: str = "jukebox", channel: str = "$jukebox"):
    irc = FakeIrc(nick)
    playlist = Playlist()
    woke: list[bool] = []
    skipped: list[bool] = []
    handler = CommandHandler(
        irc, playlist, channel, lambda: woke.append(True), lambda: skipped.append(True)
    )
    return handler, irc, playlist, woke, skipped


def test_play_adds_and_wakes():
    handler, irc, playlist, woke, _ = _handler()
    handler.on_message("alice", "$jukebox", ".play http://x/v")
    assert [i.url for i in playlist.upcoming()] == ["http://x/v"]
    assert woke == [True]
    assert "http://x/v" in irc.sent[-1][1]


def test_play_reacts_with_msgid():
    handler, irc, playlist, woke, _ = _handler()
    handler.on_message("alice", "$jukebox", ".play http://x/v", msgid="abc123")
    assert [i.url for i in playlist.upcoming()] == ["http://x/v"]
    assert woke == [True]
    assert irc.reacted == [("$jukebox", "abc123", "✅")]
    assert irc.sent == []  # the reaction is the ack; no redundant text reply


def test_skip_calls_skip():
    handler, _, _, _, skipped = _handler()
    handler.on_message("alice", "$jukebox", ".skip")
    assert skipped == [True]


def test_clear_empties_queue():
    handler, _, playlist, _, _ = _handler()
    playlist.add("http://x/1")
    handler.on_message("alice", "$jukebox", ".clear")
    assert playlist.upcoming() == []


def test_now_reports_current():
    handler, irc, playlist, _, _ = _handler()
    playlist.add("http://x/1", title="Song")
    playlist.take_next()
    handler.on_message("alice", "$jukebox", ".now")
    assert "Song" in irc.sent[-1][1]


def test_queue_lists_upcoming():
    handler, irc, playlist, _, _ = _handler()
    playlist.add("http://x/1", title="A")
    handler.on_message("alice", "$jukebox", ".queue")
    assert "A" in irc.sent[-1][1]


def test_pm_is_ignored():
    handler, irc, playlist, woke, _ = _handler()
    handler.on_message("alice", "jukebox", ".play http://x/v")
    assert playlist.upcoming() == []
    assert irc.sent == []
    assert woke == []


def test_self_echo_is_ignored():
    handler, _, playlist, _, _ = _handler()
    handler.on_message("jukebox", "$jukebox", ".play http://x/v")
    assert playlist.upcoming() == []


def test_self_echo_uses_live_nick():
    handler, irc, playlist, _, _ = _handler()
    irc.nick = "jukebox_"  # 433 fallback changed it after connect
    handler.on_message("jukebox_", "$jukebox", ".play http://x/v")
    assert playlist.upcoming() == []


def test_non_command_is_ignored():
    handler, irc, playlist, _, _ = _handler()
    handler.on_message("alice", "$jukebox", "hello world")
    assert playlist.upcoming() == []
    assert irc.sent == []


def test_play_without_url_replies_usage():
    handler, irc, playlist, _, _ = _handler()
    handler.on_message("alice", "$jukebox", ".play")
    assert playlist.upcoming() == []
    assert "usage" in irc.sent[-1][1].lower()
