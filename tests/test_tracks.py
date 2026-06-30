import array
import math

import av

from obby_jukebox import tracks
from obby_jukebox.tracks import AudioMeter, JukeboxAudioTrack, JukeboxVideoTrack


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
    frame = await track.recv()
    assert (frame.width, frame.height) == (320, 240)
    assert frame.format.name == "yuv420p"
    assert any(h > 0 for h in track._bars)  # loud audio drives the bars up


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


async def test_video_track_emits_fallback_without_source():
    track = JukeboxVideoTrack(320, 240, fps=30)
    frame = await track.recv()
    assert (frame.width, frame.height) == (320, 240)
    assert frame.pts == 0
    nxt = await track.recv()
    assert nxt.pts == 3000  # 90000 / 30
