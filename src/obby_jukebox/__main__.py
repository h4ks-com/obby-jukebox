"""Entrypoint: wire the IRC client, the WebRTC publisher, and the REST API into
one asyncio process. If the IRC connection drops the process exits non-zero and
the orchestrator restarts the single pod."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import uvicorn

from obby_jukebox.api import create_app
from obby_jukebox.commands import CommandHandler
from obby_jukebox.config import Settings
from obby_jukebox.fallback import FallbackShow
from obby_jukebox.ircconn import IrcClient
from obby_jukebox.jellyfin import JellyfinClient
from obby_jukebox.player import Playlist
from obby_jukebox.publisher import Publisher

logger = logging.getLogger(__name__)

VOICE_CAPS = ["message-tags", "server-time", "account-tag", "obsidianirc/voice"]


async def _init_fallback(fallback: FallbackShow, series: str) -> None:
    try:
        await fallback.set_series(series)
    except (LookupError, OSError, ValueError) as e:
        logger.warning("could not start default fallback %r: %s", series, e)


async def _run() -> None:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # aiortc/aioice log every RTP packet at DEBUG, which buries app logs.
    for noisy in ("aiortc", "aioice", "av", "libav"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    playlist = Playlist(maxlen=settings.max_queue)
    irc = IrcClient(
        host=settings.irc_host,
        port=settings.irc_port,
        tls=settings.irc_tls,
        nick=settings.irc_nick,
        sasl_user=settings.irc_sasl_user,
        sasl_pass=settings.irc_sasl_pass,
        caps=VOICE_CAPS,
    )
    jellyfin = JellyfinClient(settings.jellyfin_url, settings.jellyfin_api_key)
    fallback = FallbackShow(jellyfin)
    publisher = Publisher(irc, settings, playlist, fallback)
    admins = {
        a.strip().casefold() for a in settings.admin_accounts.split(",") if a.strip()
    }
    irc.on_message = CommandHandler(
        irc,
        playlist,
        settings.voice_channel,
        publisher.wake,
        publisher.skip,
        fallback,
        admins,
    ).on_message

    app = create_app(playlist, publisher.wake, publisher.skip, api_key=settings.api_key)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.http_bind,
            port=settings.http_port,
            log_level=settings.log_level,
        )
    )

    await irc.connect()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    # start() is short-lived (join handshake then returns); only the long-running
    # tasks (or a shutdown signal) should trigger teardown.
    start_task = asyncio.create_task(publisher.start())
    stop_task = asyncio.create_task(stop.wait())
    serving = [asyncio.create_task(irc.run()), asyncio.create_task(server.serve())]
    side_tasks = [start_task]
    if settings.jellyfin_api_key and settings.fallback_series:
        side_tasks.append(
            asyncio.create_task(_init_fallback(fallback, settings.fallback_series))
        )

    await asyncio.wait([stop_task, *serving], return_when=asyncio.FIRST_COMPLETED)
    await publisher.stop()
    irc.quit("shutting down")
    await asyncio.sleep(0.5)  # flush QUIT before the socket closes
    for task in (stop_task, *serving, *side_tasks):
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(stop_task, *serving, *side_tasks)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
