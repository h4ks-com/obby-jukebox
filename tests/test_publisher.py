import asyncio
import os
import subprocess
import time
import types
from typing import cast
from unittest.mock import Mock

import av
import pytest

from obby_jukebox import publisher
from obby_jukebox.config import Settings
from obby_jukebox.player import Playlist
from obby_jukebox.publisher import (
    Publisher,
    _download,
    _ffmpeg_seek_cmd,
    _open_player,
)


class _FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


def test_ffmpeg_seek_rebases_timestamps_to_zero():
    cmd = _ffmpeg_seek_cmd("http://x/v.mp4", 5)
    # genpts + avoid_negative_ts re-base PTS after the cut so the encoder doesn't
    # stutter on the discontinuity.
    assert cmd[cmd.index("-avoid_negative_ts") + 1] == "make_zero"
    assert cmd[cmd.index("-fflags") + 1] == "+genpts"


def test_ffmpeg_seek_is_an_input_seek_with_stream_copy():
    cmd = _ffmpeg_seek_cmd("http://x/v.mp4", 90.5)
    # -ss before -i is an input seek (the point); -c copy avoids a re-encode.
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-ss") + 1] == "90.500"
    assert cmd[cmd.index("-i") + 1] == "http://x/v.mp4"
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert cmd[-1] == "pipe:1"


def test_ffmpeg_seek_forces_av_interleave():
    # max_interleave_delta 0 keeps a transcode's audio from racing ahead of video.
    cmd = _ffmpeg_seek_cmd("u", 30)
    assert cmd[cmd.index("-max_interleave_delta") + 1] == "0"


def test_ffmpeg_seek_skips_input_seek_when_server_positioned():
    # offset 0: the server (Jellyfin) already positioned the stream, so ffmpeg
    # only remuxes — no -ss — but still re-interleaves A/V.
    cmd = _ffmpeg_seek_cmd("http://jf/stream.mkv", 0)
    assert "-ss" not in cmd
    assert cmd[cmd.index("-i") + 1] == "http://jf/stream.mkv"
    assert cmd[cmd.index("-max_interleave_delta") + 1] == "0"
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


def test_position_is_none_when_idle():
    assert _publisher().position() is None


def test_position_counts_offset_plus_elapsed():
    pub = _publisher()
    pub._play_offset = 10.0
    pub._play_started = time.monotonic() - 2.0
    pos = pub.position()
    assert pos is not None and 11.5 < pos < 12.5


def test_set_idle_clears_position():
    pub = _publisher()
    pub._play_started = time.monotonic()
    pub._set_idle()
    assert pub.position() is None


def test_download_copies_without_reencode(monkeypatch):
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd
        return _FakeProc(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(os.path, "getsize", lambda _p: 4096)
    assert _download("http://x/v.mp4", "/tmp/out.mkv") is True
    cmd = captured["cmd"]
    assert cmd[cmd.index("-c") + 1] == "copy"  # no re-encode → no CPU spike
    assert cmd[cmd.index("-i") + 1] == "http://x/v.mp4"
    assert cmd[-1] == "/tmp/out.mkv"


def test_download_fails_on_timeout(monkeypatch):
    def boom(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", boom)
    assert _download("http://x/v", "/tmp/out.mkv") is False


def test_download_fails_on_empty_output(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **_k: _FakeProc(0))
    monkeypatch.setattr(os.path, "getsize", lambda _p: 0)
    assert _download("http://x/v", "/tmp/out.mkv") is False


async def test_buffer_returns_local_path_on_success(monkeypatch):
    pub = _publisher()
    monkeypatch.setattr(publisher, "_download", lambda _url, _path: True)
    path = await pub._buffer("http://x/v")
    assert path is not None and path.endswith(".mkv")
    os.unlink(path)


async def test_buffer_cleans_up_and_returns_none_on_failure(monkeypatch):
    pub = _publisher()
    unlinked: list[str] = []
    real_unlink = os.unlink

    def record(path: str) -> None:
        unlinked.append(path)
        real_unlink(path)

    monkeypatch.setattr(os, "unlink", record)
    monkeypatch.setattr(publisher, "_download", lambda _url, _path: False)
    assert await pub._buffer("http://x/v") is None
    assert unlinked and unlinked[0].endswith(".mkv")  # the temp file was removed


async def test_buffer_removes_temp_when_cancelled(monkeypatch):
    pub = _publisher()
    unlinked: list[str] = []
    real_unlink = os.unlink

    def record(path: str) -> None:
        unlinked.append(path)
        real_unlink(path)

    async def cancel_mid_wait(_work, _max_wait):
        raise asyncio.CancelledError

    monkeypatch.setattr(os, "unlink", record)
    monkeypatch.setattr(publisher, "_download", lambda _url, _path: False)
    monkeypatch.setattr(pub, "_with_skip", cancel_mid_wait)
    with pytest.raises(asyncio.CancelledError):
        await pub._buffer("http://x/v")
    assert unlinked and unlinked[0].endswith(".mkv")  # cleaned up despite cancel


async def test_with_skip_true_when_work_finishes():
    pub = _publisher()

    async def quick() -> int:
        return 42

    work = asyncio.ensure_future(quick())
    assert await pub._with_skip(work, 5) is True
    assert work.result() == 42


async def test_with_skip_false_when_skip_fires():
    pub = _publisher()

    async def slow() -> int:
        await asyncio.sleep(10)
        return 1

    work = asyncio.ensure_future(slow())
    pub._skip.set()
    assert await pub._with_skip(work, 5) is False
    work.cancel()
