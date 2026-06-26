# obby-jukebox

IRC video-stream jukebox: a `yt-dlp` queue published to an ObbyIRCd voice/stream channel over
WebRTC (aiortc), managed by a small REST API. Single process, in-memory queue. See `PLAN.md` for the
protocol details (IRC handshake + the `+obsidianirc/rtc` WebRTC-over-TAGMSG signaling).

## Commands
- `uv sync` — install
- `uv run pytest` — tests
- `uv run ruff check .` / `uv run ruff format .`
- `uv run mypy` — types (strict)
- `pre-commit run -a` — all checks

## Conventions
- Python 3.14, uv-managed, `src/obby_jukebox/` layout.
- Modern static types: `dict[k, v]`, `X | None`, `def f[T]()`. No `Any` for records — use a dataclass/
  TypedDict/`BaseModel`. Type hints on signatures, not locals.
- Imports at module top level only.
- No bare `except` / `except Exception` — catch the specific errors that can be raised.
- Comments explain *why*, never *what*; default to none. No change-narration comments.
- Less is better: build only what's asked; no speculative features.

## Layout
- `config.py` — env-var settings · `ircconn.py` — IRC client · `signaling.py` — WebRTC-over-IRC codec
- `publisher.py` — aiortc peer · `player.py` — queue + yt-dlp + decode · `api.py` — REST
