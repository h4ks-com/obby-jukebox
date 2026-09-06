import array
import asyncio
import math
from typing import cast

import av
import pytest
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError

from obby_jukebox import tracks
from obby_jukebox.tracks import AudioMeter, JukeboxAudioTrack, JukeboxVideoTrack


class _StalledSource(MediaStreamTrack):
    kind = "video"

    async def recv(self) -> av.VideoFrame:
        await asyncio.sleep(3600)
        raise MediaStreamError


class _CountingSource(MediaStreamTrack):
    """A source that hands out frames every `interval` seconds and counts them."""

    kind = "video"

    def __init__(self, width: int, height: int, interval: float = 1 / 60) -> None:
        super().__init__()
        self._width = width
        self._height = height
        self._interval = interval
        self.served = 0

    async def recv(self) -> av.VideoFrame:
        await asyncio.sleep(self._interval)
        self.served += 1
        return av.VideoFrame(width=self._width, height=self._height, format="yuv420p")


def _tone_frame(amplitude: int, samples: int = 960) -> av.AudioFrame:
    frame = av.AudioFrame(format="s16", layout="stereo", samples=samples)
    values = array.array(
        "h", [int(amplitude * math.sin(i / 8)) for i in range(samples * 2)]
    )
    frame.planes[0].update(values.tobytes())
    return frame


def test_silent_frame_format():
    f = tracks._silent_frame()
    assert f.format.name == "s16"
    assert f.sample_rate == 48000
    assert f.samples == 960


def test_idle_frame_format():
    f = tracks._frame_from_image(tracks._idle_image(640, 360))
    assert (f.width, f.height) == (640, 360)
    assert f.format.name == "yuv420p"


async def test_audio_track_emits_silence_without_source():
    track = JukeboxAudioTrack(AudioMeter())
    frame = await track.recv()
    assert frame.sample_rate == 48000
    assert frame.pts == 0
    nxt = await track.recv()
    assert nxt.pts == frame.samples  # pts stays monotonic


def test_frame_rms_scales_with_amplitude():
    assert tracks._frame_rms(_tone_frame(0)) == 0.0
    loud = tracks._frame_rms(_tone_frame(30000))
    quiet = tracks._frame_rms(_tone_frame(3000))
    assert loud > quiet > 0.0
    assert loud <= 1.0


def test_audio_meter_attacks_fast_and_releases_slow():
    meter = AudioMeter()
    for _ in range(20):
        meter.push(0.5)
    loud = meter.level
    assert loud > 0.3
    meter.push(0.0)
    # One silent frame must not drop the level all the way back to zero.
    assert 0.0 < meter.level < loud


async def test_video_track_renders_visualizer_when_audio_only():
    meter = AudioMeter()
    for _ in range(20):
        meter.push(0.6)
    track = JukeboxVideoTrack(320, 240, fps=30, meter=meter)
    track.show_visualizer()
    track._vis_style = 0  # the bars style is the one that drives _bars
    frame = await track.recv()
    assert (frame.width, frame.height) == (320, 240)
    assert frame.format.name == "yuv420p"
    assert any(h > 0 for h in track._bars)  # loud audio drives the bars up


async def test_every_visualizer_style_renders_a_valid_frame():
    meter = AudioMeter()
    for _ in range(10):
        meter.push(0.6)
    for style in range(tracks._VIS_STYLES):
        track = JukeboxVideoTrack(320, 240, fps=30, meter=meter)
        track.show_visualizer()
        track._vis_style = style
        for _ in range(3):  # several frames so stateful styles (starfield) advance
            frame = await track.recv()
            assert (frame.width, frame.height) == (320, 240)
            assert frame.format.name == "yuv420p"


def test_show_visualizer_picks_a_style_in_range():
    track = JukeboxVideoTrack(320, 240, fps=30, meter=AudioMeter())
    track.show_visualizer()
    assert 0 <= track._vis_style < tracks._VIS_STYLES


def test_change_visualizer_cycles_and_selects():
    track = JukeboxVideoTrack(320, 240, fps=30, meter=AudioMeter())
    track.show_visualizer()
    track._vis_style = 0
    assert track.change_visualizer() == "mirror"  # None cycles 0 → 1
    assert track.change_visualizer(3) == "wave"  # index selects
    assert track.change_visualizer(len(tracks.VIS_NAMES)) == "bars"  # wraps


def test_change_visualizer_noop_when_hidden():
    track = JukeboxVideoTrack(320, 240, fps=30, meter=AudioMeter())
    assert track.change_visualizer() is None  # nothing to change while hidden


async def test_visualizer_off_falls_back_to_idle_card():
    track = JukeboxVideoTrack(320, 240, fps=30, meter=AudioMeter())
    track.show_visualizer()
    track.hide_visualizer()
    await track.recv()
    assert track._vis_tick == 0  # no visualizer frames rendered while hidden


def test_video_letterbox_keeps_fixed_output_size():
    track = JukeboxVideoTrack(640, 360, fps=30)
    wide = track._letterbox(av.VideoFrame(320, 100, "yuv420p"))
    assert (wide.width, wide.height) == (640, 360)
    assert wide.format.name == "yuv420p"
    tall = track._letterbox(av.VideoFrame(100, 320, "yuv420p"))
    assert (tall.width, tall.height) == (640, 360)


async def test_video_recv_falls_back_to_idle_when_source_stalls():
    track = JukeboxVideoTrack(320, 240, fps=30, meter=AudioMeter())
    track.set_source(_StalledSource())
    started_at = track.last_frame_at
    frame = await asyncio.wait_for(
        track.recv(), timeout=tracks._SOURCE_RECV_TIMEOUT + 2
    )
    assert (frame.width, frame.height) == (320, 240)
    assert track.last_frame_at == started_at  # no real frame ever arrived


async def test_video_track_emits_fallback_without_source():
    track = JukeboxVideoTrack(320, 240, fps=30)
    frame = await track.recv()
    assert (frame.width, frame.height) == (320, 240)
    assert frame.pts == 0
    nxt = await track.recv()
    # Timestamps follow the wall clock, so an idle frame lands about one frame
    # interval on rather than exactly 90000/30 ticks.
    assert nxt.pts is not None
    assert 1500 < nxt.pts < 6000


def test_frame_deadline_holds_a_steady_cadence():
    # Folding each overshoot into the next interval silently cost a third of the
    # frame rate, so a frame arriving late must not push the following one out.
    track = JukeboxVideoTrack(320, 240, fps=30)
    track._due = 100.0
    track._next_due(100.02)
    assert track._due == pytest.approx(100.0 + 1 / 30)
    track._next_due(100.04)
    assert track._due == pytest.approx(100.0 + 2 / 30)
    # Falling far behind restarts the cadence rather than bursting to catch up.
    track._next_due(200.0)
    assert track._due == pytest.approx(200.0 + 1 / 30)


async def test_video_track_drops_a_source_running_faster_than_the_channel():
    # A 60fps source must not spend the encoder's budget on frames we have no
    # room to send: half of them are dropped so the rest keep their detail.
    track = JukeboxVideoTrack(320, 240, fps=30)
    track.set_source(cast(MediaStreamTrack, _CountingSource(320, 240)))
    for _ in range(6):
        await track.recv()
    source = cast(_CountingSource, track._source)
    # Without pacing each recv would consume exactly one source frame.
    assert source.served > 6, f"forwarded every frame of a 2x source ({source.served})"


async def test_video_track_keeps_every_frame_of_a_slow_source():
    track = JukeboxVideoTrack(320, 240, fps=30)
    track.set_source(cast(MediaStreamTrack, _CountingSource(320, 240, interval=0.05)))
    for _ in range(4):
        await track.recv()
    source = cast(_CountingSource, track._source)
    assert source.served == 4
