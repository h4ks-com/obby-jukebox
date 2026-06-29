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
import contextlib
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
from obby_jukebox.fallback import FallbackShow
from obby_jukebox.ircconn import IrcClient
from obby_jukebox.player import Item, Playlist, Resolved, resolve
from obby_jukebox.signaling import (
    RTC_TAG,
    Reassembler,
    Signal,
    encode_signal,
    parse_rtc_tag,
)
from obby_jukebox.tracks import JukeboxAudioTrack, JukeboxVideoTrack

logger = logging.getLogger(__name__)

# Hard ceiling on a single resolve so a pathological URL (e.g. a whole channel
# yt-dlp tries to enumerate) can never wedge the media loop.
_RESOLVE_TIMEOUT = 30


class Publisher:
    def __init__(
        self,
        irc: IrcClient,
        settings: Settings,
        playlist: Playlist,
        fallback: FallbackShow,
    ) -> None:
        self.irc = irc
        self.s = settings
        self.playlist = playlist
        self.fallback = fallback
        self.channel = settings.voice_channel
        self._reasm = Reassembler()
        self._self_join = asyncio.Event()
        self._skip = asyncio.Event()
        self._wake = asyncio.Event()
        self._fallback_reload = asyncio.Event()
        self._pc: RTCPeerConnection | None = None
        self._audio: JukeboxAudioTrack | None = None
        self._video: JukeboxVideoTrack | None = None
        self._media_started = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._pending_ice: list[Signal] = []
        self._role = ""

    def wake(self) -> None:
        self._wake.set()

    def skip(self) -> None:
        self._skip.set()

    def reload_fallback(self) -> None:
        """Drop the fallback episode that's playing now so a `.show` change or
        `.show off` takes effect immediately instead of at the episode's end.
        Wakes the loop too, so a change made while idle also starts at once."""
        self._fallback_reload.set()
        self._wake.set()

    async def stop(self) -> None:
        """Leave the channel, cancel the media loop, and close the PC so the SFU
        drops our peer cleanly instead of lingering as a ghost streamer — and so a
        reconnect doesn't leave the old decode pipeline running."""
        with contextlib.suppress(RuntimeError):
            self._send({"type": "leave", "channel": self.channel})
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._pc is not None:
            await self._pc.close()
            self._pc = None

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
            elif kind == "role":
                await self._on_role(sig)
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
        self._role = sig.get("role") or "streamer"
        logger.info("joined %s as %s; negotiating webrtc", self.channel, self._role)
        if self._role != "streamer":
            logger.warning(
                "joined %s as viewer (someone else holds the streamer slot); "
                "our tracks are dropped until they leave and we're promoted",
                self.channel,
            )

        @pc.on("connectionstatechange")
        def _conn_state() -> None:
            logger.info("pc connection=%s", pc.connectionState)

        @pc.on("iceconnectionstatechange")
        def _ice_state() -> None:
            logger.info("pc ice=%s", pc.iceConnectionState)

        self._audio = JukeboxAudioTrack()
        self._video = JukeboxVideoTrack(
            self.s.video_width,
            self.s.video_height,
            self.s.video_fps,
            idle_image=self.s.idle_image,
        )
        pc.addTrack(self._audio)
        pc.addTrack(self._video)
        await pc.setLocalDescription(await pc.createOffer())
        offer_lines = encode_signal(
            self.channel, {"type": "offer", "sdp": pc.localDescription.sdp}
        )
        logger.info("sending offer in %d tagmsg chunk(s)", len(offer_lines))
        for line in offer_lines:
            self.irc.send_raw(line)
        # Always-on: the fallback card streams immediately, so clients render a
        # tile even before anything is queued.
        self._send({"type": "video", "state": "on"})
        if not self._media_started:
            self._media_started = True
            self._spawn(self._media_loop())

    async def _on_answer(self, sig: Signal) -> None:
        if self._pc is None:
            return
        logger.info("answer received; applying remote description")
        await self._pc.setRemoteDescription(
            RTCSessionDescription(sdp=sig["sdp"], type="answer")
        )
        pending, self._pending_ice = self._pending_ice, []
        for cand in pending:
            await self._add_ice(cand)

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
        if self._pc.remoteDescription is None:
            self._pending_ice.append(sig)  # flushed once the answer is applied
            return
        await self._add_ice(sig)

    async def _add_ice(self, sig: Signal) -> None:
        if self._pc is None:
            return
        candidate = candidate_from_sdp(sig.get("cand", "").removeprefix("candidate:"))
        candidate.sdpMid = sig.get("mid")
        candidate.sdpMLineIndex = sig.get("mlineidx")
        await self._pc.addIceCandidate(candidate)

    async def _on_role(self, sig: Signal) -> None:
        if sig.get("member", "").casefold() != self.irc.nick.casefold():
            return
        if sig.get("role") == "streamer" and self._role != "streamer":
            logger.info("promoted to streamer; re-publishing")
            self._role = "streamer"
            await self._republish()

    async def _republish(self) -> None:
        """Re-join so the SFU ingests our tracks as a streamer. Tracks we sent
        while a viewer were dropped, and a promotion alone won't re-trigger
        ingestion, so we leave and rejoin to renegotiate from a clean slate."""
        old = self._pc
        self._pc = None
        self._pending_ice.clear()
        with contextlib.suppress(RuntimeError):
            self._send({"type": "leave", "channel": self.channel})
        if old is not None:
            await old.close()
        await asyncio.sleep(0.5)
        self._send({"type": "join", "channel": self.channel})

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
                await self._play_fallback_or_idle()
            else:
                await self._play_item(item)

    async def _play_fallback_or_idle(self) -> None:
        episode = self.fallback.peek()
        if episode is None:
            self._set_idle()
            await self._wake.wait()
            self._wake.clear()
            return
        self._wake.clear()
        self._fallback_reload.clear()
        await self._play_resolved(episode, interruptible=True)
        # A .show change/off interrupted this episode: re-peek the new state
        # without advancing, so a freshly chosen show starts at its own episode.
        if self._fallback_reload.is_set():
            self._fallback_reload.clear()
            return
        # Move to the next episode unless a user queued something (resume the
        # show where we left off after their items play). Natural end, a .skip,
        # or a failed open all advance, so a bad episode can't wedge the channel.
        if not self.playlist.upcoming():
            self.fallback.advance()
        await asyncio.sleep(0.2)  # guard against a hot loop on repeated failures

    async def _play_item(self, item: Item) -> None:
        logger.info("resolving: %s", item.url)
        resolved = await self._resolve(item.url)
        if resolved is None:
            return
        item.title = item.title or resolved.title
        await self._play_resolved(
            Resolved(resolved.media_url, item.title), interruptible=False
        )

    async def _resolve(self, url: str) -> Resolved | None:
        """Resolve in a worker thread, but never let it block the loop: a .skip
        or a timeout abandons it (the thread can't be cancelled, so it drains in
        the background) and playback moves on."""
        work = asyncio.ensure_future(
            asyncio.to_thread(resolve, url, self.s.ytdlp_cookies)
        )
        skip = asyncio.ensure_future(self._skip.wait())
        try:
            done, _ = await asyncio.wait(
                {work, skip},
                timeout=_RESOLVE_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            skip.cancel()
        if work not in done:
            work.add_done_callback(_ignore_result)
            logger.warning("resolve abandoned for %s (skipped or timed out)", url)
            return None
        try:
            return work.result()
        except (DownloadError, KeyError, OSError) as e:
            logger.warning("resolve failed for %s: %s", url, e)
            return None

    async def _play_resolved(self, resolved: Resolved, *, interruptible: bool) -> None:
        try:
            source = MediaPlayer(resolved.media_url)
        except (OSError, ValueError) as e:
            logger.warning("open failed for %s: %s", resolved.title, e)
            return
        logger.info("now playing: %s", resolved.title)
        try:
            if self._audio is not None and source.audio is not None:
                self._audio.set_source(source.audio)
            if self._video is not None and source.video is not None:
                self._video.set_source(source.video)
            self._send({"type": "video", "state": "on"})
            await self._await_end(source, interruptible=interruptible)
        finally:
            self._set_idle()
            _stop_player(source)

    async def _await_end(self, source: MediaPlayer, *, interruptible: bool) -> None:
        track = source.video or source.audio
        while track is not None and track.readyState == "live":
            if self._skip.is_set():
                return
            # A queued user request, or a .show change/off, preempts the
            # fallback show immediately (user items aren't interruptible).
            if interruptible and (
                self.playlist.upcoming() or self._fallback_reload.is_set()
            ):
                return
            await asyncio.sleep(0.5)


def _ignore_result(task: asyncio.Task[Resolved]) -> None:
    """Retrieve an abandoned task's outcome so a late failure isn't logged as an
    unretrieved exception."""
    if not task.cancelled():
        task.exception()


def _stop_player(source: MediaPlayer) -> None:
    """Stop a MediaPlayer's tracks so its libav container + worker thread are
    released; otherwise each played item leaks a running decode pipeline."""
    for track in (source.audio, source.video):
        if track is not None:
            track.stop()
