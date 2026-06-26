"""Entrypoint: wire the IRC client, the WebRTC publisher, and the REST API into
one asyncio process. If the IRC connection drops the process exits non-zero and
the orchestrator restarts the single pod."""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from obby_jukebox.api import create_app
from obby_jukebox.config import Settings
from obby_jukebox.ircconn import IrcClient
from obby_jukebox.player import Playlist
from obby_jukebox.publisher import Publisher

VOICE_CAPS = ["message-tags", "server-time", "obsidianirc/voice"]


async def _run() -> None:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

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
    publisher = Publisher(irc, settings, playlist)

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
    async with asyncio.TaskGroup() as tg:
        tg.create_task(irc.run())
        tg.create_task(publisher.start())
        tg.create_task(server.serve())


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
