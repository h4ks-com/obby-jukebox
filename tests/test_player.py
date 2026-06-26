from unittest.mock import MagicMock, patch

import pytest

from obby_jukebox.player import Playlist, QueueFull, resolve


def test_add_and_upcoming():
    pl = Playlist()
    a = pl.add("u1", "one")
    b = pl.add("u2")
    assert [i.url for i in pl.upcoming()] == ["u1", "u2"]
    assert a.id != b.id
    assert pl.now is None


def test_take_next_sets_now_and_drains():
    pl = Playlist()
    pl.add("u1")
    pl.add("u2")
    first = pl.take_next()
    assert first is not None and first.url == "u1"
    assert pl.now is first
    second = pl.take_next()
    assert second is not None and second.url == "u2"
    assert pl.take_next() is None
    assert pl.now is None


def test_clear():
    pl = Playlist()
    pl.add("u1")
    pl.clear()
    assert pl.upcoming() == []


def test_queue_full():
    pl = Playlist(maxlen=2)
    pl.add("u1")
    pl.add("u2")
    with pytest.raises(QueueFull):
        pl.add("u3")


def test_resolve_uses_ytdlp():
    fake = MagicMock()
    fake.__enter__.return_value.extract_info.return_value = {
        "url": "https://cdn/stream.mp4",
        "title": "Cool Video",
        "is_live": False,
    }
    with patch("obby_jukebox.player.yt_dlp.YoutubeDL", return_value=fake) as ydl:
        out = resolve("https://youtu.be/x", cookies="/c.txt")
    assert out.media_url == "https://cdn/stream.mp4"
    assert out.title == "Cool Video"
    assert out.is_live is False
    assert ydl.call_args.args[0]["cookiefile"] == "/c.txt"
