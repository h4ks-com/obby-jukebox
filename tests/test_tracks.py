from obby_jukebox import tracks
from obby_jukebox.tracks import JukeboxAudioTrack, JukeboxVideoTrack


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
    track = JukeboxAudioTrack()
    frame = await track.recv()
    assert frame.sample_rate == 48000
    assert frame.pts == 0
    nxt = await track.recv()
    assert nxt.pts == frame.samples  # pts stays monotonic


async def test_video_track_emits_fallback_without_source():
    track = JukeboxVideoTrack(320, 240, fps=30)
    frame = await track.recv()
    assert (frame.width, frame.height) == (320, 240)
    assert frame.pts == 0
    nxt = await track.recv()
    assert nxt.pts == 3000  # 90000 / 30
