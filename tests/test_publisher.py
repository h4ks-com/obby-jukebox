import asyncio
import types
from typing import cast
from unittest.mock import Mock

import av
import pytest

from obby_jukebox import publisher
from obby_jukebox.config import Settings
from obby_jukebox.player import Playlist
from obby_jukebox.publisher import Publisher, _ffmpeg_seek_cmd, _open_player


def test_ffmpeg_seek_is_an_input_seek_with_stream_copy():
    cmd = _ffmpeg_seek_cmd("http://x/v.mp4", 90.5)
    # -ss before -i is an input seek (the point); -c copy avoids a re-encode.
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-ss") + 1] == "90.500"
    assert cmd[cmd.index("-i") + 1] == "http://x/v.mp4"
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert cmd[-1] == "pipe:1"


def test_open_player_skips_source_that_403s(monkeypatch):
    def raise_403(*_args, **_kwargs):
        raise av.error.HTTPForbiddenError(858797304, "403", "http://x/v.mp4")

    monkeypatch.setattr(publisher, "MediaPlayer", raise_403)
    assert _open_player("http://x/v.mp4") is None


def _publisher() -> Publisher:
    settings = cast(Settings, types.SimpleNamespace(voice_channel="$tv"))
    return Publisher(Mock(), settings, Playlist(), Mock())


async def test_media_loop_survives_a_failed_item():
    pub = _publisher()
    calls = 0

    async def play_next() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise av.error.HTTPForbiddenError(858797304, "403", "http://x/v.mp4")
        raise asyncio.CancelledError

    pub._play_next = play_next  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await pub._media_loop()
    assert calls == 2
