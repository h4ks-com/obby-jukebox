from obby_jukebox import signaling
from obby_jukebox.signaling import Reassembler, Signal


def test_escape_roundtrip():
    raw = "a;b c\\d\r\ne"
    assert signaling.unescape_tag_value(signaling.escape_tag_value(raw)) == raw


def test_unescape_trailing_backslash_dropped():
    assert signaling.unescape_tag_value("abc\\") == "abc"


def test_encode_small_signal_single_line():
    lines = signaling.encode_signal("$vc", {"type": "join", "channel": "$vc"})
    assert len(lines) == 1
    prefix, _, target = lines[0].partition(" TAGMSG ")
    assert target == "$vc"
    value = prefix.removeprefix("@" + signaling.RTC_TAG + "=")
    assert signaling.parse_rtc_tag(signaling.unescape_tag_value(value)) == {
        "type": "join",
        "channel": "$vc",
    }


def test_encode_escapes_spaces_in_values():
    lines = signaling.encode_signal("$vc", {"type": "error", "error": "no good"})
    assert "\\s" in lines[0]  # the space inside the error string is escaped


def test_encode_large_offer_is_chunked():
    big: Signal = {"type": "offer", "sdp": "v=0\r\n" + "a=x;y z\r\n" * 4000}
    lines = signaling.encode_signal("$vc", big)
    assert len(lines) > 1
    assert all(len(line) <= signaling.WIRE_BUDGET + 64 for line in lines)


def test_chunk_reassembles_to_original_sdp():
    sdp = "v=0\r\n" + "a=candidate foo;bar baz\r\n" * 4000
    big: Signal = {"type": "offer", "sdp": sdp}
    chunks = signaling._chunk(big)
    assert len(chunks) > 1
    assert {c["seq"] for c in chunks} == set(range(len(chunks)))
    assert all(c["total"] == len(chunks) for c in chunks)
    assert all(c["id"] == chunks[0]["id"] for c in chunks)

    re = Reassembler()
    out = None
    for c in chunks:
        out = re.feed(c)
    assert out is not None
    assert out["sdp"] == sdp
    assert out["type"] == "offer"
    assert "seq" not in out and "total" not in out and "id" not in out


def test_reassembler_passes_through_unchunked():
    re = Reassembler()
    sig: Signal = {"type": "answer", "sdp": "v=0"}
    assert re.feed(sig) == sig


def test_reassembler_returns_none_until_complete():
    re = Reassembler()
    a: Signal = {"type": "offer", "id": "x", "seq": 0, "total": 2, "sdp": "aa"}
    b: Signal = {"type": "offer", "id": "x", "seq": 1, "total": 2, "sdp": "bb"}
    assert re.feed(a) is None
    out = re.feed(b)
    assert out is not None and out["sdp"] == "aabb"


def test_parse_rtc_tag():
    assert signaling.parse_rtc_tag('{"type":"joined","members":[]}') == {
        "type": "joined",
        "members": [],
    }
    assert signaling.parse_rtc_tag("not json") is None
    assert signaling.parse_rtc_tag('{"no":"type"}') is None
