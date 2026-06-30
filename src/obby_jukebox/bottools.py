"""draft/bot-cmds + draft/bot-tools: announce the bot's commands and accept
structured invocations over TAGMSG (https://ircv3.net/specs/extensions/bot-tools).

Clients send a `+draft/bot-cmds-query` and get back the command catalog on
`+draft/bot-cmds` (compact JSON, base64); a `+draft/bot-cmd` carries an
invocation we route through the normal command pipeline. Pairs with the `+B`
bot mode set in IrcClient."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Protocol

from obby_jukebox.commands import COMMANDS, Command

CAPS = ["bot-mode", "draft/bot-cmds", "draft/bot-tools", "batch", "draft/message-ids"]

# (sender, target, text, msgid, account) — matches CommandHandler.on_message.
InvokeFn = Callable[[str, str, str, str | None, str | None], None]


class _Irc(Protocol):
    def tagmsg(self, target: str, tags: dict[str, str]) -> None: ...


def _encode(obj: object) -> str:
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.b64encode(raw).decode("ascii")


def _decode(value: str) -> object | None:
    try:
        data: object = json.loads(base64.b64decode(value))
    except ValueError:
        return None
    return data


def _schema(command: Command) -> dict[str, object]:
    options = []
    if command.args:
        options.append(
            {
                "name": "text",
                "type": "string",
                "required": False,
                "description": command.args,
            }
        )
    return {
        "name": command.name,
        "description": command.summary,
        "contexts": ["public", "pm"],
        "options": options,
    }


def catalog(prefix: str) -> dict[str, object]:
    return {"prefix": prefix, "commands": [_schema(c) for c in COMMANDS]}


class BotTools:
    def __init__(self, irc: _Irc, channel: str, prefix: str, invoke: InvokeFn) -> None:
        self._irc = irc
        self._channel = channel
        self._prefix = prefix
        self._invoke = invoke

    def handle_tagmsg(
        self, sender: str, target: str, tags: dict[str, str | None]
    ) -> bool:
        """Handle a bot-cmds TAGMSG, returning True when it was one of ours."""
        if "+draft/bot-cmds-query" in tags:
            self._irc.tagmsg(
                sender, {"+draft/bot-cmds": _encode(catalog(self._prefix))}
            )
            return True
        if "+draft/bot-cmd" in tags:
            self._dispatch(sender, tags)
            return True
        return False

    def announce_changed(self, channel: str) -> None:
        self._irc.tagmsg(channel, {"+draft/bot-cmds-changed": ""})

    def _dispatch(self, sender: str, tags: dict[str, str | None]) -> None:
        payload = _decode(tags.get("+draft/bot-cmd") or "")
        if not isinstance(payload, dict):
            return
        name = payload.get("name")
        if not isinstance(name, str):
            return
        options = payload.get("options")
        text = options.get("text", "") if isinstance(options, dict) else ""
        line = f"{self._prefix}{name} {text}".strip()
        self._invoke(
            sender, self._channel, line, tags.get("msgid"), tags.get("account")
        )
