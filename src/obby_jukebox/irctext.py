"""Inline IRC formatting (mIRC control codes) for channel replies."""

from __future__ import annotations

_BOLD = "\x02"
_COLOR = "\x03"
_RESET = "\x0f"

RED = 4
ORANGE = 7
TEAL = 10
GREY = 14


def bold(text: str) -> str:
    return f"{_BOLD}{text}{_BOLD}"


def color(text: str, fg: int) -> str:
    return f"{_COLOR}{fg:02d}{text}{_RESET}"
