"""Entrypoint: wire the IRC client, the WebRTC publisher, and the REST API into
one asyncio process. The IRC link is supervised — a dropped connection is retried
with backoff while the in-memory queue and fallback position survive — so a blip
doesn't need a pod restart."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import uvicorn

from obby_jukebox.api import create_app
from obby_jukebox.bottools import CAPS as BOT_CAPS
from obby_jukebox.bottools import BotTools
from obby_jukebox.commands import CommandHandler
from obby_jukebox.config import Settings
from obby_jukebox.fallback import FallbackShow
from obby_jukebox.ircconn import IrcClient
from obby_jukebox.jellyfin import JellyfinClient
from obby_jukebox.player import Playlist, SearchCache
from obby_jukebox.publisher import Publisher

logger = logging.getLogger(__name__)

VOICE_CAPS = ["message-tags", "server-time", "account-tag", "obsidianirc/voice"]
CAPS = VOICE_CAPS + BOT_CAPS
_MAX_BACKOFF = 60


async def _init_fallback(fallback: FallbackShow, series: str) -> None:
    try:
        await fallback.set_series(series)
    except (LookupError, OSError, ValueError) as e:
        logger.warning("could not start default fallback %r: %s", series, e)


async def _sleep_unless_stopped(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


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
    jellyfin = JellyfinClient(
        settings.jellyfin_url,
        settings.jellyfin_api_key,
        burn_subtitles=settings.jellyfin_burn_subtitles,
    )
    fallback = FallbackShow(jellyfin)
    search_cache = SearchCache()
    admins = {
        a.strip().casefold() for a in settings.admin_accounts.split(",") if a.strip()
    }
    # The REST API outlives any single IRC connection; it routes wake/skip to
    # whichever publisher is currently connected.
    live: dict[str, Publisher | None] = {"publisher": None}

    def wake() -> None:
        if live["publisher"] is not None:
            live["publisher"].wake()

    def skip() -> None:
        if live["publisher"] is not None:
            live["publisher"].skip()

    def seek(seconds: float) -> None:
        if live["publisher"] is not None:
            live["publisher"].seek(seconds)

    app = create_app(playlist, wake, skip, seek, api_key=settings.api_key)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.http_bind,
            port=settings.http_port,
            log_level=settings.log_level,
        )
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    server_task = asyncio.create_task(server.serve())
    fallback_init: asyncio.Task[None] | None = None
    backoff = 1.0

    while not stop.is_set():
        irc = IrcClient(
            host=settings.irc_host,
            port=settings.irc_port,
            tls=settings.irc_tls,
            nick=settings.irc_nick,
            sasl_user=settings.irc_sasl_user,
            sasl_pass=settings.irc_sasl_pass,
            caps=CAPS,
            register=settings.irc_register,
            register_email=settings.irc_register_email,
        )
        publisher = Publisher(irc, settings, playlist, fallback)
        handler = CommandHandler(
            irc,
            playlist,
            settings.voice_channel,
            publisher.wake,
            publisher.skip,
            publisher.seek,
            publisher.reload_fallback,
            fallback,
            admins,
            search_cache,
            cookies=settings.ytdlp_cookies,
        )
        irc.on_message = handler.on_message
        irc.bottools = BotTools(irc, settings.voice_channel, ".", handler.on_message)
        live["publisher"] = publisher

        try:
            await irc.connect()
        except OSError as e:
            logger.warning("IRC connect failed: %s; retrying in %ss", e, backoff)
            await _sleep_unless_stopped(stop, backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)
            continue

        start_task = asyncio.create_task(publisher.start())
        run_task = asyncio.create_task(irc.run())
        # Start the configured default show once Jellyfin answers, retried on each
        # reconnect until it sticks. The task is kept out of the teardown below so
        # a blip mid-fetch doesn't strand the channel without a default.
        if (
            settings.fallback_series
            and jellyfin.configured
            and not fallback.active
            and (fallback_init is None or fallback_init.done())
        ):
            fallback_init = asyncio.create_task(
                _init_fallback(fallback, settings.fallback_series)
            )
        stop_task = asyncio.create_task(stop.wait())

        await asyncio.wait([run_task, stop_task], return_when=asyncio.FIRST_COMPLETED)

        await publisher.stop()
        irc.quit("shutting down" if stop.is_set() else "reconnecting")
        await asyncio.sleep(0.3)  # flush QUIT before the socket closes
        live["publisher"] = None
        connection_tasks = [start_task, run_task, stop_task]
        for task in connection_tasks:
            task.cancel()
        await asyncio.gather(*connection_tasks, return_exceptions=True)

        if not stop.is_set():
            # Reset only after a session that actually registered; a server that
            # accepts the socket then drops us pre-registration keeps backing off.
            if irc.registered.is_set():
                backoff = 1.0
            logger.warning("IRC link lost; reconnecting in %ss", backoff)
            await _sleep_unless_stopped(stop, backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)

    if fallback_init is not None and not fallback_init.done():
        fallback_init.cancel()
        await asyncio.gather(fallback_init, return_exceptions=True)
    server.should_exit = True
    await asyncio.gather(server_task, return_exceptions=True)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
