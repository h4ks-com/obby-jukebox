"""REST control surface for the jukebox queue."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from obby_jukebox.fallback import FallbackShow
from obby_jukebox.player import Item, Playlist, QueueFull, Resolved

# Whatever ffmpeg can open directly, which covers every MediaMTX output (RTSP,
# RTMP, SRT, WebRTC-adjacent HLS) as well as plain files and icecast.
_STREAM_SCHEMES = frozenset(
    {"http", "https", "rtsp", "rtsps", "rtmp", "rtmps", "srt", "udp"}
)


class AddRequest(BaseModel):
    url: str
    title: str = ""


class ItemOut(BaseModel):
    id: str
    url: str
    title: str
    duration: int | None = None


class QueueOut(BaseModel):
    now: ItemOut | None
    upcoming: list[ItemOut]


class SeekRequest(BaseModel):
    seconds: float


class FallbackResource(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=240)
    live: bool = False
    # A stream that never ends holds the channel forever unless it is given a
    # slot; omit it for one that should play out in full.
    max_seconds: float | None = Field(default=None, gt=0, le=86400)

    @field_validator("url")
    @classmethod
    def playable(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in _STREAM_SCHEMES or not parts.netloc:
            raise ValueError(f"unsupported stream URL: {value!r}")
        return value

    @property
    def is_live(self) -> bool:
        # A stream protocol has no end and no seekable position; treating one as
        # a file wedges the media loop buffering an infinite source.
        return self.live or urlsplit(self.url).scheme not in ("http", "https")


class FallbackRequest(BaseModel):
    resources: list[FallbackResource] = Field(default_factory=list, max_length=100)


class FallbackResourceOut(BaseModel):
    url: str
    title: str
    live: bool
    max_seconds: float | None = None


class TvState(BaseModel):
    now: ItemOut | None
    position: float | None
    fallback: str | None
    queue: list[ItemOut]
    stream_url: str | None
    stream_title: str | None
    stream_kind: str | None
    fallback_queue: list[str]


class FallbackUpdate(BaseModel):
    status: str
    count: int


def _out(item: Item) -> ItemOut:
    return ItemOut(id=item.id, url=item.url, title=item.title, duration=item.duration)


def _programme(
    resources: list[Resolved],
) -> list[tuple[str, str, bool, float | None]]:
    return [(r.media_url, r.title, r.live, r.max_seconds) for r in resources]


def create_app(
    playlist: Playlist,
    wake: Callable[[], None],
    skip: Callable[[], None],
    seek: Callable[[float], None],
    fallback: FallbackShow | None = None,
    position: Callable[[], float | None] = lambda: None,
    api_key: str = "",
    current: Callable[[], Resolved | None] = lambda: None,
    reload_fallback: Callable[[], None] = lambda: None,
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

    @app.get("/tv/state", response_model=TvState)
    def tv_state() -> TvState:
        cur = playlist.now
        fallback_title = fallback.now_label() if fallback and fallback.active else None
        source = current()
        return TvState(
            now=(
                ItemOut(id="stream", url=source.media_url, title=source.title)
                if source
                else (
                    _out(cur)
                    if cur
                    else (
                        ItemOut(id="fallback", url="", title=fallback_title)
                        if fallback_title
                        else None
                    )
                )
            ),
            position=position(),
            fallback=fallback.status() if fallback else None,
            queue=[_out(item) for item in playlist.upcoming()],
            stream_url=source.media_url if source else None,
            stream_title=source.title if source else None,
            stream_kind=("audio" if source.audio_only else "video") if source else None,
            fallback_queue=fallback.queue_labels() if fallback else [],
        )

    @app.get("/", include_in_schema=False)
    @app.get("/tv", include_in_schema=False)
    def tv_page() -> FileResponse:
        return FileResponse(_TV_TEMPLATE)

    @app.put("/fallback", dependencies=[Depends(auth)])
    def set_fallback(req: FallbackRequest) -> FallbackUpdate:
        if fallback is None:
            raise HTTPException(status_code=503, detail="fallback unavailable")
        resources = [
            Resolved(
                item.url,
                item.title,
                live=item.is_live,
                max_seconds=item.max_seconds,
            )
            for item in req.resources
        ]
        # A controller polls with the same programme over and over; re-airing it
        # every time would cut the stream on every poll.
        if _programme(fallback.external()) == _programme(resources):
            return FallbackUpdate(status="unchanged", count=len(resources))
        fallback.set_external(resources)
        # A new programme goes on air now; a human request still outranks it.
        reload_fallback()
        return FallbackUpdate(status="updated", count=len(resources))

    @app.get(
        "/fallback",
        dependencies=[Depends(auth)],
        response_model=list[FallbackResourceOut],
    )
    def get_fallback() -> list[FallbackResourceOut]:
        if fallback is None:
            return []
        return [
            FallbackResourceOut(
                url=item.media_url,
                title=item.title,
                live=item.live,
                max_seconds=item.max_seconds,
            )
            for item in fallback.external()
        ]

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


_TV_TEMPLATE = Path(__file__).parent / "templates" / "tv.html"
