import httpx

from obby_jukebox.jellyfin import JellyfinClient


def _client(payload: dict[str, object], burn_subtitles: bool = True) -> JellyfinClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JellyfinClient(
        "http://jf", "key", burn_subtitles=burn_subtitles, client=client
    )


async def test_search_series_parses_fields():
    c = _client({"Items": [{"Id": "s1", "Name": "Arcane", "ProductionYear": 2021}]})
    results = await c.search_series("arcane")
    assert results[0].id == "s1"
    assert results[0].name == "Arcane"
    assert results[0].year == 2021


async def test_episodes_tolerate_missing_fields():
    c = _client({"Items": [{"Id": "e1"}, {"Name": "no id is dropped to empty"}]})
    eps = await c.episodes("s1")
    assert eps[0].id == "e1"
    assert eps[0].season == 0  # missing ParentIndexNumber → 0, not a crash
    assert eps[0].number == 0
    assert eps[1].id == ""


async def test_empty_payload_is_empty_list():
    c = _client({})
    assert await c.search_series("x") == []


async def test_episodes_sorted_by_season_then_number():
    c = _client(
        {
            "Items": [
                {"Id": "b", "ParentIndexNumber": 2, "IndexNumber": 1},
                {"Id": "a", "ParentIndexNumber": 1, "IndexNumber": 2},
                {"Id": "c", "ParentIndexNumber": 1, "IndexNumber": 1},
            ]
        }
    )
    eps = await c.episodes("s1")
    assert [(e.season, e.number) for e in eps] == [(1, 1), (1, 2), (2, 1)]


def _ep_with_streams(streams: list[dict[str, object]]) -> dict[str, object]:
    return {
        "Id": "e1",
        "ParentIndexNumber": 1,
        "IndexNumber": 1,
        "MediaStreams": streams,
    }


async def test_episodes_pick_english_subtitle_index():
    c = _client(
        {
            "Items": [
                _ep_with_streams(
                    [
                        {"Type": "Audio", "Index": 1, "Language": "eng"},
                        {"Type": "Subtitle", "Index": 3, "Language": "spa"},
                        {"Type": "Subtitle", "Index": 2, "Language": "eng"},
                    ]
                )
            ]
        }
    )
    eps = await c.episodes("s1")
    assert eps[0].subtitle_index == 2


async def test_episodes_prefer_full_default_over_forced_english_subtitle():
    c = _client(
        {
            "Items": [
                _ep_with_streams(
                    [
                        {
                            "Type": "Subtitle",
                            "Index": 4,
                            "Language": "eng",
                            "IsForced": True,
                        },
                        {
                            "Type": "Subtitle",
                            "Index": 5,
                            "Language": "en",
                            "IsDefault": True,
                        },
                    ]
                )
            ]
        }
    )
    eps = await c.episodes("s1")
    assert eps[0].subtitle_index == 5


async def test_episodes_no_english_subtitle_is_none():
    c = _client(
        {
            "Items": [
                _ep_with_streams([{"Type": "Subtitle", "Index": 2, "Language": "spa"}])
            ]
        }
    )
    eps = await c.episodes("s1")
    assert eps[0].subtitle_index is None


async def test_burn_subtitles_off_ignores_streams():
    c = _client(
        {
            "Items": [
                _ep_with_streams([{"Type": "Subtitle", "Index": 2, "Language": "eng"}])
            ]
        },
        burn_subtitles=False,
    )
    eps = await c.episodes("s1")
    assert eps[0].subtitle_index is None


def test_stream_url_direct_play_without_subtitle():
    c = _client({})
    assert c.stream_url("abc") == "http://jf/Videos/abc/stream?static=true&api_key=key"


def test_stream_url_burns_subtitle_when_index_given():
    c = _client({})
    url = c.stream_url("abc", 2)
    assert url == (
        "http://jf/Videos/abc/stream.mkv?api_key=key&Static=false"
        "&SubtitleStreamIndex=2&SubtitleMethod=Encode&VideoCodec=h264&AudioCodec=aac"
        "&VideoBitrate=8000000"
    )


def test_stream_url_adds_start_time_ticks_when_seeking_transcode():
    c = _client({})
    url = c.stream_url("abc", 2, start_seconds=90)
    assert url.endswith("&StartTimeTicks=900000000")  # 90s in 100ns ticks


def test_stream_url_carries_play_session_id_for_a_fresh_transcode():
    c = _client({})
    url = c.stream_url("abc", 2, start_seconds=90, play_session_id="sess1")
    assert "StartTimeTicks=900000000" in url
    assert url.endswith("&PlaySessionId=sess1")


def test_stream_url_seeking_a_direct_item_switches_to_transcode():
    c = _client({})
    # No subtitle but seeking: a direct stream can't be seeked server-side, so
    # the seek re-requests a (subtitle-free) transcode beginning at the offset.
    url = c.stream_url("abc", start_seconds=90)
    assert "stream.mkv" in url and "Static=false" in url
    assert "SubtitleStreamIndex" not in url
    assert "StartTimeTicks=900000000" in url


async def test_season_episode_counts():
    c = _client(
        {
            "Items": [
                {"Id": "a", "ParentIndexNumber": 1},
                {"Id": "b", "ParentIndexNumber": 1},
                {"Id": "c", "ParentIndexNumber": 2},
            ]
        }
    )
    assert await c.season_episode_counts("s1") == {1: 2, 2: 1}


async def test_search_movies_parses_fields_and_subtitle():
    c = _client(
        {
            "Items": [
                {
                    "Id": "m1",
                    "Name": "Inception",
                    "ProductionYear": 2010,
                    "MediaStreams": [
                        {"Type": "Subtitle", "Index": 2, "Language": "eng"}
                    ],
                }
            ]
        }
    )
    movies = await c.search_movies("inception")
    assert movies[0].id == "m1"
    assert movies[0].name == "Inception"
    assert movies[0].year == 2010
    assert movies[0].subtitle_index == 2


def test_configured_tracks_api_key():
    assert _client({}).configured
    assert not JellyfinClient("http://jf", "").configured
