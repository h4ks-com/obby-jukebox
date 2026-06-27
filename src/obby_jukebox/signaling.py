"""The `+obsidianirc/rtc` WebRTC-over-IRC signaling: tag escaping, the envelope
schema, and SDP chunking (offers/answers exceed the server's message-tag limit).

All signaling is JSON carried in the ``+obsidianirc/rtc`` tag of a ``TAGMSG`` to a
voice/stream channel. See PLAN.md for the full exchange.
"""

from __future__ import annotations

import json
import uuid
from typing import TypedDict

RTC_TAG = "+obsidianirc/rtc"

# Max escaped tag-value length per TAGMSG. The server caps client tags at 8191;
# stay well under so the whole line (verb + channel + tag) fits.
WIRE_BUDGET = 7800


class TurnConfig(TypedDict):
    urls: list[str]
    username: str
    password: str


class Signal(TypedDict, total=False):
    type: str
    channel: str
    sdp: str
    cand: str
    mid: str
    mlineidx: int
    state: str
    turn: TurnConfig
    error: str
    # SDP chunking
    id: str
    seq: int
    total: int


_ESCAPE = str.maketrans(
    {";": "\\:", " ": "\\s", "\\": "\\\\", "\r": "\\r", "\n": "\\n"}
)


def escape_tag_value(value: str) -> str:
    return value.translate(_ESCAPE)


def _dumps(signal: Signal) -> str:
    return json.dumps(signal, separators=(",", ":"))


def encode_signal(channel: str, signal: Signal) -> list[str]:
    """Encode a signal into one or more raw ``TAGMSG`` lines (chunked if the
    escaped value would exceed WIRE_BUDGET)."""
    return [
        f"@{RTC_TAG}={escape_tag_value(_dumps(chunk))} TAGMSG {channel}"
        for chunk in _chunk(signal)
    ]


def _chunk(signal: Signal) -> list[Signal]:
    if len(escape_tag_value(_dumps(signal))) <= WIRE_BUDGET:
        return [signal]

    sdp = signal.get("sdp", "")
    if not sdp:  # oversized but nothing chunkable; send as-is and let it fail loudly
        return [signal]

    cid = signal.get("id") or uuid.uuid4().hex[:12]
    base: Signal = {k: v for k, v in signal.items() if k != "sdp"}  # type: ignore[assignment]
    base["id"] = cid
    overhead = len(
        escape_tag_value(_dumps({**base, "seq": 999, "total": 999, "sdp": ""}))
    )
    # Escaping at most doubles length; keep raw pieces safely under the remaining room.
    piece_len = max(1, (WIRE_BUDGET - overhead) // 2)

    pieces = [sdp[i : i + piece_len] for i in range(0, len(sdp), piece_len)]
    chunks: list[Signal] = []
    for seq, piece in enumerate(pieces):
        chunk: Signal = {**base, "seq": seq, "total": len(pieces), "sdp": piece}
        chunks.append(chunk)
    return chunks


class Reassembler:
    """Reassembles chunked offer/answer signals; passes non-chunked signals through."""

    def __init__(self) -> None:
        self._pending: dict[str, dict[int, str]] = {}

    def feed(self, signal: Signal) -> Signal | None:
        total = signal.get("total")
        cid = signal.get("id")
        if total is None or cid is None:
            return signal

        parts = self._pending.setdefault(cid, {})
        parts[signal.get("seq", 0)] = signal.get("sdp", "")
        if len(parts) < total:
            return None

        del self._pending[cid]
        sdp = "".join(parts[i] for i in range(total))
        whole: Signal = {
            k: v for k, v in signal.items() if k not in ("seq", "total", "id")
        }  # type: ignore[assignment]
        whole["sdp"] = sdp
        return whole


def parse_rtc_tag(raw_value: str) -> Signal | None:
    """Decode an already-unescaped ``+obsidianirc/rtc`` tag value into a Signal."""
    try:
        obj = json.loads(raw_value)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "type" not in obj:
        return None
    return obj  # type: ignore[return-value]
