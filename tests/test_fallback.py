import httpx
import pytest

from obby_jukebox.fallback import FallbackShow
from obby_jukebox.jellyfin import JellyfinClient

SERIES = [{"Id": "s1", "Name": "Breaking Bad", "ProductionYear": 2008}]
EPISODES = [
    {"Id": "e1", "ParentIndexNumber": 1, "IndexNumber": 1, "Name": "Pilot"},
    {"Id": "e2", "ParentIndexNumber": 1, "IndexNumber": 2, "Name": "Cat's in the Bag"},
    {"Id": "e3", "ParentIndexNumber": 2, "IndexNumber": 1, "Name": "737"},
]


def _title(fb: FallbackShow) -> str:
    episode = fb.peek()
    assert episode is not None
    return episode.title


def _fallback(series: list[dict[str, object]] | None = None) -> FallbackShow:
    found = SERIES if series is None else series

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("IncludeItemTypes") == "Series":
            return httpx.Response(200, json={"Items": found})
        return httpx.Response(200, json={"Items": EPISODES})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return FallbackShow(JellyfinClient("http://jf", "key", client=client))


def _fallback_with(episodes: list[dict[str, object]]) -> FallbackShow:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("IncludeItemTypes") == "Series":
            return httpx.Response(200, json={"Items": SERIES})
        return httpx.Response(200, json={"Items": episodes})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return FallbackShow(JellyfinClient("http://jf", "key", client=client))


def _stamp(fb: FallbackShow) -> str:
    label = fb.now_label()
    assert label is not None
    return label.split(" — ")[0]


def test_inactive_peek_is_none():
    fb = _fallback()
    assert fb.peek() is None
    assert not fb.active
    assert fb.now_label() is None


async def test_sequence_and_wrap():
    fb = _fallback()
    await fb.set_series("breaking", 1, 1)
    assert _title(fb).startswith("Breaking Bad S01E01")
    fb.advance()
    assert "S01E02" in _title(fb)
    fb.advance()
    assert "S02E01" in _title(fb)
    fb.advance()
    assert "S01E01" in _title(fb)  # wraps at the end of the series


async def test_start_midway():
    fb = _fallback()
    await fb.set_series("breaking", 2, 1)
    assert "S02E01" in _title(fb)


async def test_start_at_missing_episode_clamps_forward():
    fb = _fallback()
    await fb.set_series("breaking", 1, 99)  # past S01 → next existing is S02E01
    assert "S02E01" in _title(fb)


async def test_no_match_raises():
    fb = _fallback(series=[])
    with pytest.raises(LookupError):
        await fb.set_series("nope")


def test_stream_url_built_from_episode_id():
    fb = _fallback()
    # The client builds a height-capped h264 transcode (never a raw direct-play).
    url = fb._jelly.stream_url("abc")
    assert "stream.mkv" in url and "MaxHeight" in url and "static=true" not in url


async def test_walks_every_episode_across_seasons_then_wraps():
    episodes = [
        {"Id": f"e{s}-{n}", "ParentIndexNumber": s, "IndexNumber": n, "Name": "x"}
        for s in (1, 2, 3)
        for n in (1, 2, 3)
    ]
    fb = _fallback_with(episodes)
    await fb.set_series("breaking", 1, 1)
    walked = []
    for _ in range(len(episodes)):
        walked.append(_stamp(fb))
        fb.advance()
    assert walked == [
        f"Breaking Bad S{s:02d}E{n:02d}" for s in (1, 2, 3) for n in (1, 2, 3)
    ]
    assert _stamp(fb) == "Breaking Bad S01E01"  # wrapped to the start


async def test_position_holds_until_advance():
    # The fallback only moves on advance(); a queued video interrupts playback
    # without advancing, so the show resumes at the same episode afterward.
    fb = _fallback()
    await fb.set_series("breaking", 1, 1)
    fb.advance()
    here = _stamp(fb)
    assert here == "Breaking Bad S01E02"
    assert _stamp(fb) == here  # repeated peeks don't move the cursor
    assert fb.peek() is not None
    assert _stamp(fb) == here


async def test_search_detailed_reports_season_counts():
    fb = _fallback()
    results = await fb.search_detailed("breaking")
    assert results[0].name == "Breaking Bad"
    assert results[0].seasons == {1: 2, 2: 1}


def _fallback_movies(movies: list[dict[str, object]] | None = None) -> FallbackShow:
    found = (
        [{"Id": "m1", "Name": "Inception", "ProductionYear": 2010}]
        if movies is None
        else movies
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("IncludeItemTypes") == "Movie":
            return httpx.Response(200, json={"Items": found})
        return httpx.Response(200, json={"Items": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return FallbackShow(JellyfinClient("http://jf", "key", client=client))


async def test_set_movie_is_a_single_looping_item():
    fb = _fallback_movies()
    status = await fb.set_movie("inception")
    assert "Inception" in status and "movie" in status
    assert fb.now_label() == "Inception (2010)"  # no SxxExx for a movie
    fb.advance()
    assert fb.now_label() == "Inception (2010)"  # one item wraps onto itself


async def test_set_movie_no_match_raises():
    fb = _fallback_movies(movies=[])
    with pytest.raises(LookupError):
        await fb.set_movie("nope")


async def test_peek_exposes_a_server_side_seek_url():
    fb = _fallback()
    await fb.set_series("breaking", 1, 1)
    resolved = fb.peek()
    assert resolved is not None and resolved.seek_url is not None
    seeked = resolved.seek_url(30)
    assert "StartTimeTicks=300000000" in seeked
    assert "PlaySessionId=" in seeked


async def test_peek_uses_a_fresh_session_id_each_play():
    fb = _fallback()
    await fb.set_series("breaking", 1, 1)
    first, second = fb.peek(), fb.peek()
    assert first is not None and second is not None
    assert "PlaySessionId=" in first.media_url
    assert first.media_url != second.media_url  # a fresh transcode each play


def test_configured_reflects_api_key():
    assert _fallback().configured
    assert not FallbackShow(JellyfinClient("http://jf", "")).configured


def test_radio_is_a_looping_audio_stream():
    fb = _fallback()
    status = fb.set_radio("https://radio.h4ks.com/radio")
    assert "radio.h4ks.com" in status
    assert fb.active
    resolved = fb.peek()
    assert resolved is not None
    assert resolved.media_url == "https://radio.h4ks.com/radio"
    assert resolved.seek_url is None  # a live stream isn't server-seekable
    assert resolved.live  # flags the player to skip seeking/buffering
    assert fb.now_label() == "📻 radio.h4ks.com"
    fb.advance()  # a live stream has no next; the cursor stays put
    again = fb.peek()
    assert again is not None and again.media_url == "https://radio.h4ks.com/radio"


async def test_radio_and_series_are_mutually_exclusive():
    fb = _fallback()
    fb.set_radio("https://radio.h4ks.com/radio")
    await fb.set_series("breaking", 1, 1)  # switching to a show drops the radio
    assert "Breaking Bad" in (fb.now_label() or "")
    fb.set_radio("https://radio.h4ks.com/radio")  # and back the other way
    assert fb.now_label() == "📻 radio.h4ks.com"


def test_clear_stops_radio():
    fb = _fallback()
    fb.set_radio("https://radio.h4ks.com/radio")
    fb.clear()
    assert not fb.active
    assert fb.peek() is None
