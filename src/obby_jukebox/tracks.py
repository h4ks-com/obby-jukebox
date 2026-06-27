"""Persistent WebRTC tracks fed by a switchable source and normalized to one
output format. Swapping queue items via ``replaceTrack`` otherwise makes
aiortc's encoder resampler see a format change and raise "Frame does not match
AudioResampler setup"; keeping one track whose output format never changes
avoids that. When no source is set the track emits silence / a static fallback
card, paced in real time so the bot keeps streaming (and holding the streamer
slot) between items."""

from __future__ import annotations

import asyncio
import fractions
import os

import av
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError
from PIL import Image, ImageDraw

_AUDIO_RATE = 48000
_AUDIO_LAYOUT = "stereo"
_AUDIO_FORMAT = "s16"
_AUDIO_PTIME = 0.020
_AUDIO_SAMPLES = int(_AUDIO_RATE * _AUDIO_PTIME)
_VIDEO_CLOCK = 90000


class JukeboxAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._source: MediaStreamTrack | None = None
        self._resampler = self._new_resampler()
        self._buffer: list[av.AudioFrame] = []
        self._pts = 0

    @staticmethod
    def _new_resampler() -> av.AudioResampler:
        return av.AudioResampler(
            format=_AUDIO_FORMAT, layout=_AUDIO_LAYOUT, rate=_AUDIO_RATE
        )

    def set_source(self, track: MediaStreamTrack) -> None:
        self._source = track
        self._resampler = self._new_resampler()
        self._buffer.clear()

    def clear_source(self) -> None:
        self._source = None
        self._buffer.clear()

    async def recv(self) -> av.AudioFrame:
        frame = await self._next()
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
                raw = await source.recv()
            except MediaStreamError:
                self._source = None
                continue
            if isinstance(raw, av.AudioFrame):
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
    ) -> None:
        super().__init__()
        self._source: MediaStreamTrack | None = None
        self._width = width
        self._height = height
        self._step = _VIDEO_CLOCK // fps
        self._frame_time = 1 / fps
        self._pts = 0
        # Static fallback shown whenever the queue is empty, so the channel is
        # never a black/empty tile and the bot keeps holding the streamer slot.
        # Kept as a PIL image so each recv() builds a fresh frame (reusing one
        # frame and mutating its pts in place can corrupt the encoder).
        self._idle_image = _idle_image(width, height, idle_image)

    def set_source(self, track: MediaStreamTrack) -> None:
        self._source = track

    def clear_source(self) -> None:
        self._source = None

    async def recv(self) -> av.VideoFrame:
        source = self._source
        frame: av.VideoFrame | None = None
        if source is not None:
            try:
                raw = await source.recv()
            except MediaStreamError:
                self._source = None
                raw = None
            if isinstance(raw, av.VideoFrame):
                frame = raw.reformat(
                    width=self._width, height=self._height, format="yuv420p"
                )
        if frame is None:
            await asyncio.sleep(self._frame_time)
            frame = _frame_from_image(self._idle_image)
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, _VIDEO_CLOCK)
        self._pts += self._step
        return frame


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
        "nothing playing — queue with .vplay <url>",
        anchor="mm",
        fill=(150, 150, 160),
    )
    return img


def _frame_from_image(img: Image.Image) -> av.VideoFrame:
    frame: av.VideoFrame = av.VideoFrame.from_image(img)  # type: ignore[no-untyped-call]
    return frame.reformat(format="yuv420p")
