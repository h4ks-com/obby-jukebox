"""The WebRTC publisher: drives the `+obsidianirc/rtc` handshake over IRC and
streams the playlist into one persistent video+audio sender pair.

aiortc gathers ICE into the SDP (non-trickle), so the offer carries our
candidates; we still accept the SFU's trickled candidates. Queue items are
swapped by switching the source of one persistent, format-normalized track
pair, so changing video needs no renegotiation and the encoder never sees a
format change.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine

from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.contrib.media import MediaPlayer
from aiortc.sdp import candidate_from_sdp
from yt_dlp.utils import DownloadError

from obby_jukebox.config import Settings
from obby_jukebox.ircconn import IrcClient
from obby_jukebox.player import Playlist, resolve
from obby_jukebox.signaling import (
    RTC_TAG,
    Reassembler,
    Signal,
    encode_signal,
    parse_rtc_tag,
)
from obby_jukebox.tracks import JukeboxAudioTrack, JukeboxVideoTrack

logger = logging.getLogger(__name__)


class Publisher:
    def __init__(self, irc: IrcClient, settings: Settings, playlist: Playlist) -> None:
        self.irc = irc
        self.s = settings
        self.playlist = playlist
        self.channel = settings.voice_channel
        self._reasm = Reassembler()
        self._self_join = asyncio.Event()
        self._skip = asyncio.Event()
        self._wake = asyncio.Event()
        self._pc: RTCPeerConnection | None = None
        self._audio: JukeboxAudioTrack | None = None
        self._video: JukeboxVideoTrack | None = None
        self._media_started = False
        self._tasks: set[asyncio.Task[None]] = set()

    def wake(self) -> None:
        self._wake.set()

    def skip(self) -> None:
        self._skip.set()

    def _spawn(self, coro: Coroutine[object, object, None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _send(self, sig: Signal) -> None:
        for line in encode_signal(self.channel, sig):
            self.irc.send_raw(line)

    async def start(self) -> None:
        self.irc.on_join = self._on_join
        self.irc.on_tagmsg = self._on_tagmsg
        await self.irc.registered.wait()
        self.irc.join(self.channel)
        try:
            await asyncio.wait_for(self._self_join.wait(), timeout=8)
        except TimeoutError:
            logger.warning("no self-echo JOIN within 8s; joining signal anyway")
        self._send({"type": "join", "channel": self.channel})

    def _on_join(self, nick: str, chan: str) -> None:
        if nick.casefold() == self.irc.nick.casefold() and chan == self.channel:
            self._self_join.set()

    def _on_tagmsg(self, sender: str, target: str, tags: dict[str, str | None]) -> None:
        value = tags.get(RTC_TAG)
        if not value:
            return
        sig = parse_rtc_tag(value)
        if sig is None:
            return
        whole = self._reasm.feed(sig)
        if whole is not None:
            self._spawn(self._dispatch(whole))

    async def _dispatch(self, sig: Signal) -> None:
        kind = sig.get("type")
        try:
            if kind == "joined":
                await self._on_joined(sig)
            elif kind == "answer":
                await self._on_answer(sig)
            elif kind == "offer":
                await self._on_renegotiate(sig)
            elif kind == "ice":
                await self._on_ice(sig)
            elif kind == "error":
                logger.error("signaling error: %s", sig.get("error"))
        except (ValueError, OSError) as e:
            logger.warning("handling %s signal failed: %s", kind, e)

    async def _on_joined(self, sig: Signal) -> None:
        if self._pc is not None:
            return
        ice = [RTCIceServer(urls=self.s.stun_url)]
        turn = sig.get("turn")
        if turn:
            ice.append(
                RTCIceServer(
                    urls=turn["urls"],
                    username=turn["username"],
                    credential=turn["password"],
                )
            )
        pc = RTCPeerConnection(RTCConfiguration(iceServers=ice))
        self._pc = pc
        self._audio = JukeboxAudioTrack()
        self._video = JukeboxVideoTrack(
            self.s.video_width, self.s.video_height, self.s.video_fps
        )
        pc.addTrack(self._audio)
        pc.addTrack(self._video)
        await pc.setLocalDescription(await pc.createOffer())
        self._send({"type": "offer", "sdp": pc.localDescription.sdp})
        if not self._media_started:
            self._media_started = True
            self._spawn(self._media_loop())

    async def _on_answer(self, sig: Signal) -> None:
        if self._pc is None:
            return
        await self._pc.setRemoteDescription(
            RTCSessionDescription(sdp=sig["sdp"], type="answer")
        )

    async def _on_renegotiate(self, sig: Signal) -> None:
        if self._pc is None:
            return
        await self._pc.setRemoteDescription(
            RTCSessionDescription(sdp=sig["sdp"], type="offer")
        )
        await self._pc.setLocalDescription(await self._pc.createAnswer())
        self._send({"type": "answer", "sdp": self._pc.localDescription.sdp})

    async def _on_ice(self, sig: Signal) -> None:
        if self._pc is None:
            return
        raw = sig.get("cand", "")
        candidate = candidate_from_sdp(raw.removeprefix("candidate:"))
        candidate.sdpMid = sig.get("mid")
        candidate.sdpMLineIndex = sig.get("mlineidx")
        await self._pc.addIceCandidate(candidate)

    def _set_idle(self) -> None:
        if self._audio is not None:
            self._audio.clear_source()
        if self._video is not None:
            self._video.clear_source()

    async def _media_loop(self) -> None:
        while True:
            self._skip.clear()
            item = self.playlist.take_next()
            if item is None:
                self._set_idle()
                await self._wake.wait()
                self._wake.clear()
                continue
            logger.info("resolving: %s", item.url)
            try:
                resolved = await asyncio.to_thread(
                    resolve, item.url, self.s.ytdlp_cookies
                )
            except (DownloadError, KeyError, OSError) as e:
                logger.warning("resolve failed for %s: %s", item.url, e)
                continue
            item.title = item.title or resolved.title
            try:
                source = MediaPlayer(resolved.media_url)
            except (OSError, ValueError) as e:
                logger.warning("open failed for %s: %s", item.title, e)
                continue
            logger.info("now playing: %s", item.title)
            if self._audio is not None and source.audio is not None:
                self._audio.set_source(source.audio)
            if self._video is not None and source.video is not None:
                self._video.set_source(source.video)
            self._send({"type": "video", "state": "on"})
            await self._await_end(source)

    async def _await_end(self, source: MediaPlayer) -> None:
        track = source.video or source.audio
        while (
            track is not None and track.readyState == "live" and not self._skip.is_set()
        ):
            await asyncio.sleep(0.5)
