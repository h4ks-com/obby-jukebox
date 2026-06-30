"""REST control surface for the jukebox queue."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from obby_jukebox.player import Item, Playlist, QueueFull


class AddRequest(BaseModel):
    url: str
    title: str = ""


class ItemOut(BaseModel):
    id: str
    url: str
    title: str


class QueueOut(BaseModel):
    now: ItemOut | None
    upcoming: list[ItemOut]


class SeekRequest(BaseModel):
    seconds: float


def _out(item: Item) -> ItemOut:
    return ItemOut(id=item.id, url=item.url, title=item.title)


def create_app(
    playlist: Playlist,
    wake: Callable[[], None],
    skip: Callable[[], None],
    seek: Callable[[float], None],
    api_key: str = "",
) -> FastAPI:
    app = FastAPI(title="obby-jukebox", version="0.1.0")

    def auth(x_api_key: str = Header(default="")) -> None:
        if api_key and x_api_key != api_key:
            raise HTTPException(status_code=401, detail="bad api key")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/queue", status_code=201, dependencies=[Depends(auth)])
    def add(req: AddRequest) -> ItemOut:
        try:
            item = playlist.add(req.url, req.title)
        except QueueFull as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        wake()
        return _out(item)

    @app.get("/queue", dependencies=[Depends(auth)])
    def queue() -> QueueOut:
        now = playlist.now
        return QueueOut(
            now=_out(now) if now else None,
            upcoming=[_out(i) for i in playlist.upcoming()],
        )

    @app.get("/now", dependencies=[Depends(auth)])
    def now() -> ItemOut | None:
        cur = playlist.now
        return _out(cur) if cur else None

    @app.post("/skip", dependencies=[Depends(auth)])
    def do_skip() -> dict[str, str]:
        skip()
        return {"status": "skipped"}

    @app.post("/seek", dependencies=[Depends(auth)])
    def do_seek(req: SeekRequest) -> dict[str, str]:
        seek(req.seconds)
        return {"status": "seeking", "seconds": str(req.seconds)}

    @app.post("/clear", dependencies=[Depends(auth)])
    def clear() -> dict[str, str]:
        playlist.clear()
        return {"status": "cleared"}

    return app
