from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from obby_jukebox.api import create_app
from obby_jukebox.fallback import FallbackShow
from obby_jukebox.player import Playlist, Resolved


@pytest.fixture
def ctx():
    pl = Playlist(maxlen=3)
    wake = MagicMock()
    skip = MagicMock()
    seek = MagicMock()
    client = TestClient(create_app(pl, wake, skip, seek))
    return SimpleNamespace(pl=pl, wake=wake, skip=skip, seek=seek, client=client)


def test_healthz_is_open(ctx):
    assert ctx.client.get("/healthz").json() == {"status": "ok"}


def test_add_queue_now_flow(ctx):
    r = ctx.client.post("/queue", json={"url": "u1", "title": "one"})
    assert r.status_code == 201
    assert r.json()["url"] == "u1"
    ctx.wake.assert_called_once()

    assert ctx.client.get("/now").json() is None
    assert [i["url"] for i in ctx.client.get("/queue").json()["upcoming"]] == ["u1"]


def test_queue_full_returns_409(ctx):
    for i in range(3):
        ctx.client.post("/queue", json={"url": f"u{i}"})
    assert ctx.client.post("/queue", json={"url": "x"}).status_code == 409


def test_skip_and_clear(ctx):
    ctx.client.post("/queue", json={"url": "u1"})
    assert ctx.client.post("/skip").json() == {"status": "skipped"}
    ctx.skip.assert_called_once()
    assert ctx.client.post("/clear").json() == {"status": "cleared"}
    assert ctx.client.get("/queue").json()["upcoming"] == []


def test_seek(ctx):
    assert ctx.client.post("/seek", json={"seconds": 90}).json()["status"] == "seeking"
    ctx.seek.assert_called_once_with(90.0)


def test_api_key_enforced():
    pl = Playlist()
    client = TestClient(
        create_app(pl, MagicMock(), MagicMock(), MagicMock(), api_key="secret")
    )
    assert client.post("/queue", json={"url": "u"}).status_code == 401
    ok = client.post("/queue", json={"url": "u"}, headers={"X-API-Key": "secret"})
    assert ok.status_code == 201
    # health stays open
    assert client.get("/healthz").status_code == 200


def test_tv_page_and_fallback_automation_are_separate_from_queue():
    pl = Playlist()
    fallback = MagicMock(spec=FallbackShow)
    fallback.status.return_value = "fallback: radio"
    fallback.active = False
    fallback.external.return_value = []
    fallback.queue_labels.return_value = []
    wake = MagicMock()
    reload_fallback = MagicMock()
    client = TestClient(
        create_app(
            pl,
            wake,
            MagicMock(),
            MagicMock(),
            fallback=fallback,
            api_key="secret",
            reload_fallback=reload_fallback,
        )
    )

    page = client.get("/")
    assert page.status_code == 200
    assert '<video id="video"' in page.text
    assert "<iframe" not in page.text
    assert client.get("/tv").status_code == 200
    state = client.get("/tv/state").json()
    assert state["now"] is None
    assert state["fallback"] == "fallback: radio"
    assert state["stream_title"] is None
    assert state["stream_kind"] is None

    response = client.put(
        "/fallback",
        json={
            "resources": [
                {"url": "https://radio.example/live", "title": "Radio", "live": True}
            ]
        },
        headers={"X-API-Key": "secret"},
    )
    assert response.status_code == 200
    fallback.set_external.assert_called_once()
    reload_fallback.assert_called_once()
    wake.assert_not_called()
    queue = client.get("/queue", headers={"X-API-Key": "secret"}).json()
    assert queue["upcoming"] == []


def test_fallback_accepts_stream_protocols_and_marks_them_live():
    fallback = MagicMock(spec=FallbackShow)
    fallback.external.return_value = []
    client = TestClient(
        create_app(Playlist(), MagicMock(), MagicMock(), MagicMock(), fallback=fallback)
    )

    accepted = client.put(
        "/fallback",
        json={
            "resources": [
                {
                    "url": "rtsp://mediamtx:8554/livegames",
                    "title": "livegames",
                    "max_seconds": 120,
                },
                {"url": "https://live.example/live/index.m3u8", "title": "hls"},
            ]
        },
    )
    assert accepted.status_code == 200
    resources = fallback.set_external.call_args.args[0]
    assert [r.live for r in resources] == [True, False]
    assert [r.max_seconds for r in resources] == [120, None]

    fallback.external.return_value = resources
    repeat = client.put(
        "/fallback",
        json={
            "resources": [
                {
                    "url": "rtsp://mediamtx:8554/livegames",
                    "title": "livegames",
                    "max_seconds": 120,
                },
                {"url": "https://live.example/live/index.m3u8", "title": "hls"},
            ]
        },
    )
    assert repeat.json()["status"] == "unchanged"
    fallback.set_external.assert_called_once()

    # A different slot length is a different programme, so it goes on air.
    relimited = client.put(
        "/fallback",
        json={
            "resources": [
                {
                    "url": "rtsp://mediamtx:8554/livegames",
                    "title": "livegames",
                    "max_seconds": 300,
                },
                {"url": "https://live.example/live/index.m3u8", "title": "hls"},
            ]
        },
    )
    assert relimited.json()["status"] == "updated"

    rejected = client.put(
        "/fallback",
        json={"resources": [{"url": "file:///etc/passwd", "title": "nope"}]},
    )
    assert rejected.status_code == 422


def test_web_stream_is_authoritative_for_now_showing():
    fallback = MagicMock(spec=FallbackShow)
    fallback.active = True
    fallback.now_label.return_value = "Jukebox fallback"
    fallback.status.return_value = "fallback: Jukebox fallback"
    fallback.queue_labels.return_value = ["Jukebox fallback"]
    client = TestClient(
        create_app(
            Playlist(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            fallback=fallback,
            current=lambda: Resolved(
                "https://live.example/stream", "livegames", live=True
            ),
        )
    )

    state = client.get("/tv/state").json()
    assert state["now"]["title"] == "livegames"
    assert state["fallback_queue"] == ["Jukebox fallback"]
