from obby_jukebox.publisher import _ffmpeg_seek_cmd


def test_ffmpeg_seek_is_an_input_seek_with_stream_copy():
    cmd = _ffmpeg_seek_cmd("http://x/v.mp4", 90.5)
    # -ss before -i is an input seek (the point); -c copy avoids a re-encode.
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-ss") + 1] == "90.500"
    assert cmd[cmd.index("-i") + 1] == "http://x/v.mp4"
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert cmd[-1] == "pipe:1"
