"""Persistent WebRTC tracks fed by a switchable source and normalized to one
output format. Swapping queue items via ``replaceTrack`` otherwise makes
aiortc's encoder resampler see a format change and raise "Frame does not match
AudioResampler setup"; keeping one track whose output format never changes
avoids that. When no source is set the track emits silence / a static fallback
card, paced in real time so the bot keeps streaming (and holding the streamer
slot) between items."""

from __future__ import annotations

import array
import asyncio
import fractions
import math
import os
import time

import av
import av.filter
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError
from PIL import Image, ImageDraw

_AUDIO_RATE = 48000
_AUDIO_LAYOUT = "stereo"
_AUDIO_FORMAT = "s16"
_AUDIO_PTIME = 0.020
_AUDIO_SAMPLES = int(_AUDIO_RATE * _AUDIO_PTIME)
_VIDEO_CLOCK = 90000

# A stalled source would otherwise block recv() forever and freeze the encoder;
# past this the track falls back to silence / the idle card.
_SOURCE_RECV_TIMEOUT = 1.0

# Visualizer shown for audio-only items (an mp3 has no video track, so without
# this the channel would sit on the static idle card while sound plays).
_VIS_BARS = 36
_VIS_BG = (12, 12, 20)
_VIS_GRAVITY = 0.045  # how fast a bar falls back per frame once the level drops
_METER_GAIN = 5.0  # music RMS lands around 0.1-0.3; scale it up to fill the bars


class AudioMeter:
    """Shared smoothed loudness in 0..1: the audio track feeds it a per-frame
    RMS and the video track reads it to size the visualizer bars. Fast attack,
    slow release so bars snap up on transients and settle back gently."""

    _ATTACK = 0.6
    _RELEASE = 0.08

    def __init__(self) -> None:
        self._level = 0.0

    def push(self, rms: float) -> None:
        target = min(1.0, rms * _METER_GAIN)
        rate = self._ATTACK if target > self._level else self._RELEASE
        self._level += rate * (target - self._level)

    @property
    def level(self) -> float:
        return self._level


class JukeboxAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, meter: AudioMeter) -> None:
        super().__init__()
        self._meter = meter
        self._source: MediaStreamTrack | None = None
        self._resampler = self._new_resampler()
        self._buffer: list[av.AudioFrame] = []
        self._pts = 0
        self.last_frame_at = 0.0

    @staticmethod
    def _new_resampler() -> av.AudioResampler:
        return av.AudioResampler(
            format=_AUDIO_FORMAT, layout=_AUDIO_LAYOUT, rate=_AUDIO_RATE
        )

    def set_source(self, track: MediaStreamTrack) -> None:
        self._source = track
        self._resampler = self._new_resampler()
        self._buffer.clear()
        self.last_frame_at = time.monotonic()

    def clear_source(self) -> None:
        self._source = None
        self._buffer.clear()

    async def recv(self) -> av.AudioFrame:
        frame = await self._next()
        self._meter.push(_frame_rms(frame))
        frame.pts = self._pts
        frame.sample_rate = _AUDIO_RATE
        frame.time_base = fractions.Fraction(1, _AUDIO_RATE)
        self._pts += frame.samples
        return frame

    async def _next(self) -> av.AudioFrame:
        while not self._buffer:
            source = self._source
            if source is None:
                await asyncio.sleep(_AUDIO_PTIME)
                return _silent_frame()
            try:
                raw = await asyncio.wait_for(source.recv(), _SOURCE_RECV_TIMEOUT)
            except TimeoutError:
                return _silent_frame()
            except MediaStreamError:
                self._source = None
                continue
            if isinstance(raw, av.AudioFrame):
                self.last_frame_at = time.monotonic()
                self._buffer.extend(self._resampler.resample(raw))
        return self._buffer.pop(0)


class JukeboxVideoTrack(MediaStreamTrack):
    kind = "video"

    def __init__(
        self,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        idle_image: str = "",
        meter: AudioMeter | None = None,
    ) -> None:
        super().__init__()
        self._source: MediaStreamTrack | None = None
        self._width = width
        self._height = height
        self._step = _VIDEO_CLOCK // fps
        self._frame_time = 1 / fps
        self._pts = 0
        self._graph: av.filter.Graph | None = None
        self._graph_in: av.filter.context.FilterContext | None = None
        self._graph_out: av.filter.context.FilterContext | None = None
        self._graph_key: tuple[int, int, str] | None = None
        # Static fallback shown whenever the queue is empty, so the channel is
        # never a black/empty tile and the bot keeps holding the streamer slot.
        # Kept as a PIL image so each recv() builds a fresh frame (reusing one
        # frame and mutating its pts in place can corrupt the encoder).
        self._idle_image = _idle_image(width, height, idle_image)
        self._meter = meter
        self._visualize = False
        self._bars = [0.0] * _VIS_BARS
        self._vis_tick = 0
        self._bar_colors = [_bar_color(i) for i in range(_VIS_BARS)]
        self.last_frame_at = 0.0

    def set_source(self, track: MediaStreamTrack) -> None:
        self._source = track
        self.last_frame_at = time.monotonic()

    def clear_source(self) -> None:
        self._source = None

    def show_visualizer(self) -> None:
        """Render animated bars (instead of the idle card) while an audio-only
        item plays. Resets bar heights so they grow in from nothing."""
        if not self._visualize:
            self._bars = [0.0] * _VIS_BARS
            self._vis_tick = 0
        self._visualize = True

    def hide_visualizer(self) -> None:
        self._visualize = False

    async def recv(self) -> av.VideoFrame:
        source = self._source
        frame: av.VideoFrame | None = None
        if source is not None:
            try:
                raw = await asyncio.wait_for(source.recv(), _SOURCE_RECV_TIMEOUT)
            except TimeoutError:
                raw = None
            except MediaStreamError:
                self._source = None
                raw = None
            if isinstance(raw, av.VideoFrame):
                self.last_frame_at = time.monotonic()
                frame = self._letterbox(raw)
        if frame is None:
            await asyncio.sleep(self._frame_time)
            if self._visualize and self._meter is not None:
                frame = self._render_visualizer(self._meter.level)
            else:
                frame = _frame_from_image(self._idle_image)
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, _VIDEO_CLOCK)
        self._pts += self._step
        return frame

    def _render_visualizer(self, level: float) -> av.VideoFrame:
        self._vis_tick += 1
        img = Image.new("RGB", (self._width, self._height), _VIS_BG)
        draw = ImageDraw.Draw(img)
        gap = self._width / _VIS_BARS
        bar_w = gap * 0.6
        base_y = int(self._height * 0.9)
        span = self._height * 0.74
        for i in range(_VIS_BARS):
            # A per-bar wobble (distinct speed + phase) keeps neighbours out of
            # step, and a centred profile makes the mid bars peak — so steady
            # audio still reads as a lively spectrum rather than a flat block.
            wobble = 0.55 + 0.45 * math.sin(self._vis_tick * (0.12 + 0.015 * i) + i)
            profile = 0.35 + 0.65 * math.sin(math.pi * (i + 0.5) / _VIS_BARS)
            target = level * profile * wobble
            if target > self._bars[i]:
                self._bars[i] = target
            else:
                self._bars[i] = max(target, self._bars[i] - _VIS_GRAVITY)
            height = max(2, int(self._bars[i] * span))
            x0 = gap * i + (gap - bar_w) / 2
            draw.rectangle(
                (x0, base_y - height, x0 + bar_w, base_y), fill=self._bar_colors[i]
            )
        return _frame_from_image(img)

    def _letterbox(self, raw: av.VideoFrame) -> av.VideoFrame:
        """Scale the source into the fixed output size preserving its aspect
        ratio, centered with black bars — so portrait/4:3 sources aren't
        stretched. The graph is rebuilt whenever the source geometry changes."""
        key = (raw.width, raw.height, raw.format.name)
        if key != self._graph_key or self._graph_in is None or self._graph_out is None:
            self._build_graph(raw)
            self._graph_key = key
        assert self._graph_in is not None and self._graph_out is not None
        self._graph_in.push(raw)
        out = self._graph_out.pull()
        assert isinstance(out, av.VideoFrame)
        return out

    def _build_graph(self, template: av.VideoFrame) -> None:
        graph = av.filter.Graph()
        buffer = graph.add_buffer(
            width=template.width,
            height=template.height,
            format=template.format,
            time_base=template.time_base or fractions.Fraction(1, _VIDEO_CLOCK),
        )
        scale = graph.add(
            "scale",
            f"{self._width}:{self._height}"
            ":force_original_aspect_ratio=decrease:force_divisible_by=2",
        )
        pad = graph.add(
            "pad", f"{self._width}:{self._height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
        fmt = graph.add("format", "yuv420p")
        sink = graph.add("buffersink")
        buffer.link_to(scale)
        scale.link_to(pad)
        pad.link_to(fmt)
        fmt.link_to(sink)
        graph.configure()
        self._graph = graph
        self._graph_in = buffer
        self._graph_out = sink


def _frame_rms(frame: av.AudioFrame) -> float:
    """Normalized RMS (0..1) of an interleaved s16 frame, read straight from the
    plane buffer so we don't need numpy for `to_ndarray`."""
    samples = array.array("h")
    samples.frombytes(bytes(frame.planes[0]))
    if not samples:
        return 0.0
    mean_square = sum(s * s for s in samples) / len(samples)
    return math.sqrt(mean_square) / 32768.0


def _bar_color(index: int) -> tuple[int, int, int]:
    t = index / max(1, _VIS_BARS - 1)
    return (int(30 + t * 200), int(210 - t * 150), int(190 + t * 40))


def _silent_frame() -> av.AudioFrame:
    frame = av.AudioFrame(
        format=_AUDIO_FORMAT, layout=_AUDIO_LAYOUT, samples=_AUDIO_SAMPLES
    )
    for plane in frame.planes:
        plane.update(bytes(plane.buffer_size))
    frame.sample_rate = _AUDIO_RATE
    return frame


def _idle_image(width: int, height: int, image_path: str = "") -> Image.Image:
    """The static 'idle' card: a custom image if given, else a generated banner."""
    if image_path and os.path.exists(image_path):
        return Image.open(image_path).convert("RGB").resize((width, height))
    img = Image.new("RGB", (width, height), (16, 18, 24))
    draw = ImageDraw.Draw(img)
    cx, cy = width // 2, height // 2
    draw.text((cx, cy - 16), "obby-jukebox", anchor="mm", fill=(235, 235, 235))
    draw.text(
        (cx, cy + 16),
        "nothing playing — queue with .play <url>",
        anchor="mm",
        fill=(150, 150, 160),
    )
    return img


def _frame_from_image(img: Image.Image) -> av.VideoFrame:
    frame: av.VideoFrame = av.VideoFrame.from_image(img)  # type: ignore[no-untyped-call]
    return frame.reformat(format="yuv420p")
