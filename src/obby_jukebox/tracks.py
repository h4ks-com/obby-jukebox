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
import colorsys
import fractions
import math
import os
import random
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
# Visualizer styles, indexed by _vis_style (order matches _render_visualizer's
# dispatch). One is picked at random per audio-only item; .vis changes it live.
VIS_NAMES = (
    "bars",
    "mirror",
    "radial",
    "wave",
    "pulse",
    "spiral",
    "starfield",
    "lissajous",
    "orbit",
    "tunnel",
    "grid",
)
_VIS_STYLES = len(VIS_NAMES)


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
        self._frame_time = 1 / fps
        self._pts = -1  # nothing sent yet, so the first frame may land on zero
        self._started = 0.0
        self._due = 0.0
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
        self._vis_style = 0
        self._bar_colors = [_bar_color(i) for i in range(_VIS_BARS)]
        self._stars: list[list[float]] = []  # [radius, angle] per star, for starfield
        self.last_frame_at = 0.0

    def set_source(self, track: MediaStreamTrack) -> None:
        self._source = track
        self.last_frame_at = time.monotonic()

    def clear_source(self) -> None:
        self._source = None

    def show_visualizer(self) -> None:
        """Animate the audio (instead of the idle card) while an audio-only item
        plays. A fresh random style is chosen per item for variety; bar state is
        reset so it grows in from nothing."""
        if not self._visualize:
            self._bars = [0.0] * _VIS_BARS
            self._vis_tick = 0
            self._vis_style = random.randrange(_VIS_STYLES)
        self._visualize = True

    def hide_visualizer(self) -> None:
        self._visualize = False

    def change_visualizer(self, style: int | None = None) -> str | None:
        """Switch the live audio animation: None cycles to the next style, an
        index selects one. Returns the new style's name, or None when nothing is
        being visualized (a video item is playing, or the channel is idle)."""
        if not self._visualize:
            return None
        if style is None:
            self._vis_style = (self._vis_style + 1) % _VIS_STYLES
        else:
            self._vis_style = style % _VIS_STYLES
        return VIS_NAMES[self._vis_style]

    async def recv(self) -> av.VideoFrame:
        frame = await self._paced_frame()
        now = time.monotonic()
        if self._started == 0.0:
            self._started = now
        # Timestamp against the wall clock rather than counting frames: a fixed
        # step only tells the truth for a source running at exactly our fps, and
        # a 60fps stream claimed 2.6x the media time it really had.
        pts = int((now - self._started) * _VIDEO_CLOCK)
        frame.pts = max(pts, self._pts + 1)
        frame.time_base = fractions.Fraction(1, _VIDEO_CLOCK)
        self._pts = frame.pts
        return frame

    async def _paced_frame(self) -> av.VideoFrame:
        """The freshest source frame that is actually due. A source faster than
        our output rate is drained rather than forwarded, so the encoder spends
        its whole bitrate on the frames we send instead of splitting it across
        frames the channel has no room for."""
        now = time.monotonic()
        if self._due == 0.0:
            self._due = now
        frame: av.VideoFrame | None = None
        while self._source is not None:
            raw = await self._from_source()
            if raw is None:
                break
            frame = raw
            now = time.monotonic()
            if now >= self._due:
                break
        if frame is not None:
            self._next_due(now)
            return frame
        # Sleep only what is left of the interval, so rendering a visualizer
        # frame comes out of the budget instead of being added to it.
        await asyncio.sleep(max(0.0, self._due - time.monotonic()))
        self._next_due(time.monotonic())
        if self._visualize and self._meter is not None:
            return self._render_visualizer(self._meter.level)
        return _frame_from_image(self._idle_image)

    def _next_due(self, now: float) -> None:
        """Advance the deadline by whole frame intervals. Restarting it from now
        would fold each overshoot into the next interval, which quietly cost a
        third of the frame rate."""
        self._due += self._frame_time
        if self._due <= now:
            self._due = now + self._frame_time

    async def _from_source(self) -> av.VideoFrame | None:
        source = self._source
        if source is None:
            return None
        try:
            raw = await asyncio.wait_for(source.recv(), _SOURCE_RECV_TIMEOUT)
        except TimeoutError:
            return None
        except MediaStreamError:
            self._source = None
            return None
        if not isinstance(raw, av.VideoFrame):
            return None
        self.last_frame_at = time.monotonic()
        return self._letterbox(raw)

    def _render_visualizer(self, level: float) -> av.VideoFrame:
        self._vis_tick += 1
        img = Image.new("RGB", (self._width, self._height), _VIS_BG)
        draw = ImageDraw.Draw(img)
        # Dispatch by name so VIS_NAMES stays the one place the style order lives:
        # a new style is an entry there plus a matching _draw_<name> method.
        getattr(self, f"_draw_{VIS_NAMES[self._vis_style]}")(draw, level)
        return _frame_from_image(img)

    def _spectrum(self, level: float) -> None:
        """Drive the per-bar heights from the loudness level. A distinct wobble
        per bar keeps neighbours out of step and a centred profile makes the mid
        bars peak, so steady audio still reads as a lively spectrum; gravity eases
        each bar back down once the level drops."""
        for i in range(_VIS_BARS):
            wobble = 0.55 + 0.45 * math.sin(self._vis_tick * (0.12 + 0.015 * i) + i)
            profile = 0.35 + 0.65 * math.sin(math.pi * (i + 0.5) / _VIS_BARS)
            target = level * profile * wobble
            if target > self._bars[i]:
                self._bars[i] = target
            else:
                self._bars[i] = max(target, self._bars[i] - _VIS_GRAVITY)

    def _draw_bars(self, draw: ImageDraw.ImageDraw, level: float) -> None:
        self._spectrum(level)
        gap = self._width / _VIS_BARS
        bar_w = gap * 0.6
        base_y = self._height * 0.9
        span = self._height * 0.74
        for i in range(_VIS_BARS):
            height = max(2.0, self._bars[i] * span)
            x0 = gap * i + (gap - bar_w) / 2
            draw.rectangle(
                (x0, base_y - height, x0 + bar_w, base_y), fill=self._bar_colors[i]
            )

    def _draw_mirror(self, draw: ImageDraw.ImageDraw, level: float) -> None:
        self._spectrum(level)
        gap = self._width / _VIS_BARS
        bar_w = gap * 0.6
        cy = self._height / 2
        half = self._height * 0.42
        for i in range(_VIS_BARS):
            height = max(1.0, self._bars[i] * half)
            x0 = gap * i + (gap - bar_w) / 2
            draw.rectangle(
                (x0, cy - height, x0 + bar_w, cy + height), fill=self._bar_colors[i]
            )

    def _draw_radial(self, draw: ImageDraw.ImageDraw, level: float) -> None:
        self._spectrum(level)
        cx, cy = self._width / 2, self._height / 2
        inner = min(self._width, self._height) * 0.16
        reach = min(self._width, self._height) * 0.30
        rot = self._vis_tick * 0.01
        for i in range(_VIS_BARS):
            ang = 2 * math.pi * i / _VIS_BARS + rot
            ca, sa = math.cos(ang), math.sin(ang)
            length = inner + self._bars[i] * reach
            draw.line(
                (cx + ca * inner, cy + sa * inner, cx + ca * length, cy + sa * length),
                fill=self._bar_colors[i],
                width=3,
            )

    def _draw_wave(self, draw: ImageDraw.ImageDraw, level: float) -> None:
        cy = self._height / 2
        amp = level * self._height * 0.4
        phase = self._vis_tick * 0.2
        step = max(2, self._width // 160)
        points = [
            (
                float(x),
                cy
                + amp
                * (0.4 + 0.6 * math.sin(math.pi * x / self._width))
                * math.sin(x / self._width * math.pi * 8 + phase),
            )
            for x in range(0, self._width + step, step)
        ]
        draw.line(points, fill=self._bar_colors[_VIS_BARS // 2], width=2)

    def _draw_pulse(self, draw: ImageDraw.ImageDraw, level: float) -> None:
        cx, cy = self._width / 2, self._height / 2
        span = min(self._width, self._height)
        for k in range(5):
            phase = (self._vis_tick * 0.03 + k / 5) % 1.0
            r = span * (0.08 + phase * 0.42 * (0.6 + level))
            fade = 220 * (1.0 - phase)
            ring = (int(fade / 3), int(fade), int(fade * 0.9))
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=ring, width=2)
        blob = span * 0.06 * (1.0 + level)
        draw.ellipse((cx - blob, cy - blob, cx + blob, cy + blob), fill=(60, 220, 200))

    def _draw_spiral(self, draw: ImageDraw.ImageDraw, level: float) -> None:
        cx, cy = self._width / 2, self._height / 2
        span = min(self._width, self._height)
        rot = self._vis_tick * 0.02
        dots = 96
        for arm in (0.0, math.pi):
            for i in range(dots):
                frac = i / dots
                ang = frac * math.pi * 5 + rot + arm
                rad = span * (0.02 + frac * (0.30 + 0.12 * level))
                x, y = cx + math.cos(ang) * rad, cy + math.sin(ang) * rad
                size = 1.0 + frac * 3.0 * (0.6 + level)
                color = self._bar_colors[int(frac * (_VIS_BARS - 1))]
                draw.ellipse((x - size, y - size, x + size, y + size), fill=color)

    def _draw_starfield(self, draw: ImageDraw.ImageDraw, level: float) -> None:
        cx, cy = self._width / 2, self._height / 2
        edge = math.hypot(cx, cy)
        if not self._stars:
            self._stars = [
                [random.uniform(2, edge), random.uniform(0, 2 * math.pi)]
                for _ in range(70)
            ]
        speed = 3.0 + level * 45.0
        for star in self._stars:
            near = star[0]
            star[0] += speed * (0.25 + star[0] / edge)
            if star[0] >= edge:
                star[0], star[1] = random.uniform(2, 30), random.uniform(0, 2 * math.pi)
                near = star[0]
            ca, sa = math.cos(star[1]), math.sin(star[1])
            bright = min(255, int(50 + 230 * star[0] / edge))
            draw.line(
                (cx + ca * near, cy + sa * near, cx + ca * star[0], cy + sa * star[0]),
                fill=(bright, bright, min(255, bright + 25)),
                width=1 if star[0] < edge * 0.6 else 2,
            )

    def _draw_lissajous(self, draw: ImageDraw.ImageDraw, level: float) -> None:
        cx, cy = self._width / 2, self._height / 2
        ax, ay = self._width * 0.42, self._height * 0.42
        gain = 0.35 + 0.65 * level
        phase = self._vis_tick * 0.02
        points = []
        for i in range(201):
            t = 2 * math.pi * i / 200
            points.append(
                (
                    cx + ax * gain * math.sin(3 * t + phase),
                    cy + ay * gain * math.sin(2 * t),
                )
            )
        draw.line(points, fill=_hsv(self._vis_tick * 0.004, 0.7, 1.0), width=2)

    def _draw_orbit(self, draw: ImageDraw.ImageDraw, level: float) -> None:
        cx, cy = self._width / 2, self._height / 2
        span = min(self._width, self._height) * 0.42
        rx, ry = span * (0.55 + 0.25 * level), span * 0.32
        for k in range(3):
            rot = self._vis_tick * 0.03 + k * math.pi / 3
            cr, sr = math.cos(rot), math.sin(rot)
            ring = []
            for i in range(61):
                a = 2 * math.pi * i / 60
                ex, ey = rx * math.cos(a), ry * math.sin(a)
                ring.append((cx + ex * cr - ey * sr, cy + ex * sr + ey * cr))
            draw.line(ring, fill=self._bar_colors[k * 12], width=1)
            ea = self._vis_tick * 0.16 + k * 2.1
            ex, ey = rx * math.cos(ea), ry * math.sin(ea)
            px, py = cx + ex * cr - ey * sr, cy + ex * sr + ey * cr
            draw.ellipse(
                (px - 5, py - 5, px + 5, py + 5), fill=self._bar_colors[k * 12]
            )
        nucleus = span * 0.12 * (1.0 + level)
        draw.ellipse(
            (cx - nucleus, cy - nucleus, cx + nucleus, cy + nucleus),
            fill=(255, 230, 120),
        )

    def _draw_tunnel(self, draw: ImageDraw.ImageDraw, level: float) -> None:
        cx, cy = self._width / 2, self._height / 2
        span = min(self._width, self._height) * 0.55
        sides, layers = 6, 14
        depths = sorted(
            ((k / layers + self._vis_tick * 0.008) % 1.0 for k in range(layers)),
            reverse=True,  # farthest first so the nearest ring lands on top
        )
        for depth in depths:
            rad = span * depth * (0.9 + 0.25 * level)
            twist = self._vis_tick * 0.02 + depth * 2.5
            poly = [
                (
                    cx + math.cos(2 * math.pi * s / sides + twist) * rad,
                    cy + math.sin(2 * math.pi * s / sides + twist) * rad,
                )
                for s in range(sides + 1)
            ]
            near = 1.0 - depth
            draw.line(
                poly, fill=(int(60 * near), int(150 * near), int(255 * near)), width=2
            )

    def _draw_grid(self, draw: ImageDraw.ImageDraw, level: float) -> None:
        cols, rows = 22, 12
        cell_w, cell_h = self._width / cols, self._height / rows
        radius = min(cell_w, cell_h) * 0.36
        for r in range(rows):
            for c in range(cols):
                pulse = math.sin(c * 0.5 - self._vis_tick * 0.12) * math.cos(
                    r * 0.45 + self._vis_tick * 0.05
                )
                value = (0.5 + 0.5 * pulse) * min(1.0, level * 1.6)
                if value < 0.06:
                    continue
                base = self._bar_colors[int((c / cols) * (_VIS_BARS - 1))]
                color = (
                    int(base[0] * value),
                    int(base[1] * value),
                    int(base[2] * value),
                )
                x, y = c * cell_w + cell_w / 2, r * cell_h + cell_h / 2
                draw.ellipse(
                    (x - radius, y - radius, x + radius, y + radius), fill=color
                )

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


def _hsv(hue: float, sat: float, val: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, sat, val)
    return (int(r * 255), int(g * 255), int(b * 255))


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
