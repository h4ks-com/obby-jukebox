"""Minimal asyncio IRC client: TLS connect, CAP + SASL PLAIN handshake, JOIN,
and raw TAGMSG send/receive. Just enough to carry the WebRTC signaling."""

from __future__ import annotations

import asyncio
import base64
import logging
import ssl
from collections.abc import Callable

from irctokens import Line, tokenise

logger = logging.getLogger(__name__)

TagMsgHandler = Callable[[str, str, dict[str, str | None]], None]
JoinHandler = Callable[[str, str], None]


class IrcClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        tls: bool,
        nick: str,
        sasl_user: str = "",
        sasl_pass: str = "",
        caps: list[str],
    ) -> None:
        self.host = host
        self.port = port
        self.tls = tls
        self.nick = nick
        self.sasl_user = sasl_user
        self.sasl_pass = sasl_pass
        self.caps = caps
        self.registered = asyncio.Event()
        self.on_tagmsg: TagMsgHandler | None = None
        self.on_join: JoinHandler | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self._want: set[str] = set()

    async def connect(self) -> None:
        ctx = ssl.create_default_context() if self.tls else None
        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port, ssl=ctx
        )
        self.send_raw("CAP LS 302")
        self.send_raw(f"NICK {self.nick}")
        self.send_raw(f"USER {self.nick} 0 * :obby-jukebox")

    def send_raw(self, line: str) -> None:
        if self._writer is None:
            raise RuntimeError("not connected")
        logger.debug(">> %s", line)
        self._writer.write((line + "\r\n").encode("utf-8"))

    def join(self, channel: str) -> None:
        self.send_raw(f"JOIN {channel}")

    async def run(self) -> None:
        assert self._reader is not None
        while True:
            raw = await self._reader.readline()
            if not raw:
                raise ConnectionError("server closed the connection")
            line = tokenise(raw.decode("utf-8", "replace").rstrip("\r\n"))
            self._handle(line)

    def _handle(self, line: Line) -> None:
        cmd = (line.command or "").upper()
        if cmd == "PING":
            self.send_raw("PONG :" + (line.params[-1] if line.params else ""))
        elif cmd == "CAP":
            self._handle_cap(line)
        elif cmd == "AUTHENTICATE":
            self._handle_authenticate(line)
        elif cmd in ("903", "904", "905", "906", "907"):
            self.send_raw("CAP END")
        elif cmd == "001":
            self.registered.set()
        elif cmd == "JOIN" and line.source:
            chan = line.params[0] if line.params else ""
            if self.on_join:
                self.on_join(line.hostmask.nickname, chan)
        elif cmd == "TAGMSG" and line.source:
            target = line.params[0] if line.params else ""
            if self.on_tagmsg:
                self.on_tagmsg(line.hostmask.nickname, target, dict(line.tags or {}))

    def _handle_cap(self, line: Line) -> None:
        sub = line.params[1].upper() if len(line.params) > 1 else ""
        if sub == "LS":
            available = set(line.params[-1].split())
            wanted = [c for c in self.caps if c in available]
            if self.sasl_user and "sasl" in available:
                wanted.append("sasl")
            self._want = set(wanted)
            if wanted:
                self.send_raw("CAP REQ :" + " ".join(wanted))
            else:
                self.send_raw("CAP END")
        elif sub == "ACK":
            self._want -= set(line.params[-1].split())
            if not self._want:
                if self.sasl_user:
                    self.send_raw("AUTHENTICATE PLAIN")
                else:
                    self.send_raw("CAP END")
        elif sub == "NAK":
            self.send_raw("CAP END")

    def _handle_authenticate(self, line: Line) -> None:
        if line.params and line.params[0] == "+":
            blob = f"{self.sasl_user}\0{self.sasl_user}\0{self.sasl_pass}".encode()
            self.send_raw("AUTHENTICATE " + base64.b64encode(blob).decode())
