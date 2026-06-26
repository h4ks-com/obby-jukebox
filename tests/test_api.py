from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from obby_jukebox.api import create_app
from obby_jukebox.player import Playlist


@pytest.fixture
def ctx():
    pl = Playlist(maxlen=3)
    wake = MagicMock()
    skip = MagicMock()
    client = TestClient(create_app(pl, wake, skip))
    return SimpleNamespace(pl=pl, wake=wake, skip=skip, client=client)


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


def test_api_key_enforced():
    pl = Playlist()
    client = TestClient(create_app(pl, MagicMock(), MagicMock(), api_key="secret"))
    assert client.post("/queue", json={"url": "u"}).status_code == 401
    ok = client.post("/queue", json={"url": "u"}, headers={"X-API-Key": "secret"})
    assert ok.status_code == 201
    # health stays open
    assert client.get("/healthz").status_code == 200
