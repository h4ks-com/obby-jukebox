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
    # peek/stream URL formatting is exercised once a series is set; here just
    # confirm the client builds the direct-play URL shape.
    assert (
        fb._jelly.stream_url("abc")
        == "http://jf/Videos/abc/stream?static=true&api_key=key"
    )


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


def test_configured_reflects_api_key():
    assert _fallback().configured
    assert not FallbackShow(JellyfinClient("http://jf", "")).configured
