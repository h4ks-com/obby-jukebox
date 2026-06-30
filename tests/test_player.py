from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from obby_jukebox.player import (
    Playlist,
    QueueFull,
    SearchCache,
    YtResult,
    resolve,
    search_youtube,
)


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


def _fake_ydl(info, captured=None):
    def make(opts):
        if captured is not None:
            cf = opts.get("cookiefile")
            captured["cookiefile"] = cf
            captured["content"] = Path(cf).read_text() if cf else None
        m = MagicMock()
        m.__enter__.return_value.extract_info.return_value = info
        return m

    return make


def test_resolve_uses_ytdlp():
    info = {"url": "https://cdn/stream.mp4", "title": "Cool Video"}
    with patch("obby_jukebox.player.yt_dlp.YoutubeDL", side_effect=_fake_ydl(info)):
        out = resolve("https://youtu.be/x")
    assert out.media_url == "https://cdn/stream.mp4"
    assert out.title == "Cool Video"
    assert out.duration is None


def test_resolve_captures_duration_as_int():
    info = {"url": "https://cdn/s.mp4", "title": "T", "duration": 215.7}
    with patch("obby_jukebox.player.yt_dlp.YoutubeDL", side_effect=_fake_ydl(info)):
        out = resolve("https://youtu.be/x")
    assert out.duration == 215


def test_add_carries_duration():
    pl = Playlist()
    item = pl.add("u", "t", 200)
    assert item.duration == 200
    assert pl.add("u2").duration is None


def test_resolve_copies_readonly_cookies_to_writable_temp(tmp_path):
    src = tmp_path / "ro.txt"
    src.write_text("COOKIEDATA")
    captured: dict[str, object] = {}
    info = {"url": "u", "title": "t"}
    with patch(
        "obby_jukebox.player.yt_dlp.YoutubeDL",
        side_effect=_fake_ydl(info, captured),
    ):
        resolve("https://x", cookies=str(src))
    assert captured["cookiefile"] != str(src)  # a copy, not the read-only mount
    assert captured["content"] == "COOKIEDATA"


def test_search_youtube_maps_entries():
    info = {
        "entries": [
            {"id": "abc", "title": "T1", "uploader": "U", "duration": 90},
            {
                "ie_key": "YoutubeTab",
                "url": "https://www.youtube.com/channel/UC123",
                "title": "A Channel",
            },
            {"url": "https://youtu.be/xyz", "title": "T2", "channel": "C"},
            "not a dict",
        ]
    }
    with patch("obby_jukebox.player.yt_dlp.YoutubeDL", side_effect=_fake_ydl(info)):
        results = search_youtube("kittens")
    # The channel match and the non-dict entry are both dropped; only videos stay.
    assert results == [
        YtResult("T1", "https://www.youtube.com/watch?v=abc", "U", 90),
        YtResult("T2", "https://youtu.be/xyz", "C", None),
    ]


def test_search_cache_is_per_channel_and_user():
    cache = SearchCache()
    assert cache.get("$tv", "alice") == []
    results = [YtResult("A", "http://a")]
    cache.put("$tv", "alice", results)
    assert cache.get("$tv", "alice") == results
    assert cache.get("$tv", "bob") == []
