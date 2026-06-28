import httpx

from obby_jukebox.jellyfin import JellyfinClient


def _client(payload: dict[str, object]) -> JellyfinClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JellyfinClient("http://jf", "key", client=client)


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
