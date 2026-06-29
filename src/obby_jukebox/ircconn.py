"""Minimal asyncio IRC client: TLS connect, CAP + SASL PLAIN handshake, JOIN,
and raw TAGMSG send/receive. Just enough to carry the WebRTC signaling."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import ssl
from collections.abc import Callable

from irctokens import Line, tokenise

logger = logging.getLogger(__name__)

TagMsgHandler = Callable[[str, str, dict[str, str | None]], None]
JoinHandler = Callable[[str, str], None]
MessageHandler = Callable[[str, str, str, str | None, str | None], None]


def _escape_tag_value(value: str) -> str:
    """IRCv3 message-tag value escaping (https://ircv3.net/specs/extensions/message-tags)."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\:")
        .replace(" ", "\\s")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


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
        register: bool = False,
        register_email: str = "",
    ) -> None:
        self.host = host
        self.port = port
        self.tls = tls
        self.nick = nick
        self.sasl_user = sasl_user
        self.sasl_pass = sasl_pass
        self.register = register
        self.register_email = register_email
        self.caps = caps
        self.registered = asyncio.Event()
        self.logged_in = False
        self.account = ""
        self.on_tagmsg: TagMsgHandler | None = None
        self.on_join: JoinHandler | None = None
        self.on_message: MessageHandler | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self._want: set[str] = set()
        self._caps_ls: set[str] = set()
        self.acked: set[str] = set()
        self._cap_ended = False
        self._register_attempted = False
        self._batch_seq = 0
        # Bot mode char advertised by the server (ISUPPORT BOT=<char>), if any.
        self._bot_mode: str | None = None
        self._bot_mode_sent = False

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

    def privmsg(self, target: str, text: str) -> None:
        self.send_raw(f"PRIVMSG {target} :{text}")

    def multiline_privmsg(self, target: str, lines: list[str]) -> None:
        """Send several display lines as one IRCv3 draft/multiline batch so
        clients render them as a single grouped message; fall back to one
        PRIVMSG per line when the server didn't negotiate the batch caps."""
        if not lines:
            return
        if len(lines) == 1 or not {"batch", "draft/multiline"} <= self.acked:
            for line in lines:
                self.privmsg(target, line)
            return
        self._batch_seq += 1
        ref = f"ml{self._batch_seq}"
        self.send_raw(f"BATCH +{ref} draft/multiline {target}")
        for line in lines:
            self.send_raw(f"@batch={ref} PRIVMSG {target} :{line}")
        self.send_raw(f"BATCH -{ref}")

    def tagmsg(self, target: str, tags: dict[str, str]) -> None:
        rendered = ";".join(f"{k}={_escape_tag_value(v)}" for k, v in tags.items())
        self.send_raw(f"@{rendered} TAGMSG {target}")

    def react(self, target: str, msgid: str, emoji: str) -> None:
        """React to a message with an emoji (IRCv3 +draft/react)."""
        self.tagmsg(target, {"+draft/react": emoji, "+reply": msgid})

    def quit(self, message: str = "") -> None:
        with contextlib.suppress(RuntimeError):
            self.send_raw(f"QUIT :{message}")

    async def run(self) -> None:
        assert self._reader is not None
        while True:
            raw = await self._reader.readline()
            if not raw:
                raise ConnectionError("server closed the connection")
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            logger.debug("<< %s", text)
            self._handle(tokenise(text))

    def _handle(self, line: Line) -> None:
        cmd = (line.command or "").upper()
        if cmd == "PING":
            self.send_raw("PONG :" + (line.params[-1] if line.params else ""))
        elif cmd == "CAP":
            self._handle_cap(line)
        elif cmd == "AUTHENTICATE":
            self._handle_authenticate(line)
        elif cmd == "900":  # RPL_LOGGEDIN
            self.account = line.params[2] if len(line.params) > 2 else self.sasl_user
        elif cmd == "903":  # SASL success
            self.logged_in = True
            self._end_cap()
        elif cmd in ("902", "904", "905", "906", "907"):  # SASL failed/aborted
            self._after_sasl_failure()
        elif cmd == "REGISTER":
            self._handle_register_reply(line)
        elif cmd == "FAIL":
            self._handle_fail(line)
        elif cmd == "433":  # nick in use during registration
            self.nick = self.nick + "_"
            self.send_raw(f"NICK {self.nick}")
        elif cmd == "001":
            self.registered.set()
            self._maybe_set_bot_mode()
            self._maybe_identify()
        elif cmd == "005":
            self._handle_isupport(line)
        elif cmd == "JOIN" and line.source:
            chan = line.params[0] if line.params else ""
            if self.on_join:
                self.on_join(line.hostmask.nickname, chan)
        elif cmd == "TAGMSG" and line.source:
            target = line.params[0] if line.params else ""
            if self.on_tagmsg:
                self.on_tagmsg(line.hostmask.nickname, target, dict(line.tags or {}))
        elif cmd == "PRIVMSG" and line.source:
            target = line.params[0] if line.params else ""
            text = line.params[1] if len(line.params) > 1 else ""
            tags = line.tags or {}
            if self.on_message:
                self.on_message(
                    line.hostmask.nickname,
                    target,
                    text,
                    tags.get("msgid"),
                    tags.get("account"),
                )

    def _handle_isupport(self, line: Line) -> None:
        # 005 params: <nick> TOKEN[=value]... :are supported by this server
        for token in line.params[1:-1]:
            key, _, value = token.partition("=")
            if key == "BOT" and value:
                self._bot_mode = value
        self._maybe_set_bot_mode()

    def _maybe_set_bot_mode(self) -> None:
        if self._bot_mode_sent or not self._bot_mode or not self.registered.is_set():
            return
        self.send_raw(f"MODE {self.nick} +{self._bot_mode}")
        self._bot_mode_sent = True

    def _handle_cap(self, line: Line) -> None:
        sub = line.params[1].upper() if len(line.params) > 1 else ""
        if sub == "LS":
            # CAP LS 302 advertises caps as `name` or `name=value` (e.g.
            # `sasl=PLAIN,EXTERNAL`); match on the bare name.
            self._caps_ls |= {tok.split("=", 1)[0] for tok in line.params[-1].split()}
            if len(line.params) > 3 and line.params[2] == "*":
                return  # multiline CAP LS continuation
            available = self._caps_ls
            wanted = [c for c in self.caps if c in available]
            if self.sasl_user and "sasl" in available:
                wanted.append("sasl")
            if (
                self.register
                and self.sasl_user
                and "draft/account-registration" in available
            ):
                wanted.append("draft/account-registration")
            self._want = set(wanted)
            if wanted:
                self.send_raw("CAP REQ :" + " ".join(wanted))
            else:
                self._end_cap()
        elif sub == "ACK":
            acked = set(line.params[-1].split())
            self.acked |= acked
            self._want -= acked
            if not self._want:
                if self.sasl_user and "sasl" in self.acked:
                    self.send_raw("AUTHENTICATE PLAIN")
                else:
                    self._end_cap()
        elif sub == "NAK":
            self._end_cap()

    def _handle_authenticate(self, line: Line) -> None:
        if line.params and line.params[0] == "+":
            blob = f"{self.sasl_user}\0{self.sasl_user}\0{self.sasl_pass}".encode()
            self.send_raw("AUTHENTICATE " + base64.b64encode(blob).decode())

    def _end_cap(self) -> None:
        if not self._cap_ended:
            self._cap_ended = True
            self.send_raw("CAP END")

    def _after_sasl_failure(self) -> None:
        can_register = (
            self.register
            and self.sasl_user
            and not self._register_attempted
            and "draft/account-registration" in self.acked
        )
        if not can_register:
            self._end_cap()
            return
        self._register_attempted = True
        email = self.register_email or "*"
        logger.info("SASL login failed; registering account %s", self.sasl_user)
        self.send_raw(f"REGISTER {self.sasl_user} {email} {self.sasl_pass}")

    def _handle_register_reply(self, line: Line) -> None:
        status = line.params[0].upper() if line.params else ""
        if status == "SUCCESS":
            self.logged_in = True
            self.account = self.sasl_user
            logger.info("registered and logged in as %s", self.sasl_user)
        elif status == "VERIFICATION_REQUIRED":
            logger.warning(
                "account %s needs email verification; continuing unauthenticated",
                self.sasl_user,
            )
        self._end_cap()

    def _handle_fail(self, line: Line) -> None:
        if line.params and line.params[0].upper() == "REGISTER":
            code = line.params[1] if len(line.params) > 1 else "?"
            logger.warning("registration failed (%s); continuing unauthenticated", code)
            self._end_cap()

    def _maybe_identify(self) -> None:
        """NickServ-style fallback for servers without account-registration: if we
        have credentials but SASL didn't log us in, identify to the account."""
        if self.sasl_user and self.sasl_pass and not self.logged_in:
            self.send_raw(f"IDENTIFY {self.sasl_user} {self.sasl_pass}")
