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
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Coroutine
from typing import cast

import av
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
from obby_jukebox.tracks import AudioMeter, JukeboxAudioTrack, JukeboxVideoTrack

logger = logging.getLogger(__name__)

# Hard ceiling on a single resolve so a pathological URL (e.g. a whole channel
# yt-dlp tries to enumerate) can never wedge the media loop.
_RESOLVE_TIMEOUT = 30

# A source that delivers no frame for this long is treated as dead and skipped,
# so a stalled stream can't freeze the channel on one item forever.
_STALL_TIMEOUT = 8.0

# Ceiling on buffering a remote item to local disk for seeking. A whole video
# can take a while to pull; that's an acceptable one-time cost per seeked item.
_BUFFER_TIMEOUT = 120.0

# Media failures that must skip the current item rather than kill the single
# queue-consumer loop: an expired/403 source URL, or a libav decode error.
_MEDIA_ERRORS = (av.FFmpegError, OSError)

# A buffering download that stalled or couldn't be spawned just yields no local
# copy; the item still streams (and seeks remotely) as before.
_DOWNLOAD_ERRORS = (subprocess.TimeoutExpired, OSError)


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
        self._seek = asyncio.Event()
        self._seek_target = 0.0
        self._pc: RTCPeerConnection | None = None
        self._audio: JukeboxAudioTrack | None = None
        self._video: JukeboxVideoTrack | None = None
        self._media_started = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._pending_ice: list[Signal] = []
        self._role = ""
        self._play_started = 0.0  # monotonic when the current source began, 0 if idle
        self._play_offset = 0.0  # seek offset the current source started from
        # Fired when playback switches to a new item, so the channel can announce it.
        self.on_track_change: Callable[[], None] | None = None

    def wake(self) -> None:
        self._wake.set()

    def skip(self) -> None:
        self._skip.set()

    def seek(self, seconds: float) -> None:
        """Jump the currently-playing item to an absolute offset; no-op when idle."""
        self._seek_target = max(0.0, seconds)
        self._seek.set()

    def position(self) -> float | None:
        """Seconds elapsed into the current item, or None when nothing plays."""
        if not self._play_started:
            return None
        return self._play_offset + (time.monotonic() - self._play_started)

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
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        # Surface a crashed background task instead of letting it die silently
        # as an unretrieved-exception warning at GC time.
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.error("background task failed", exc_info=exc)

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

        meter = AudioMeter()
        self._audio = JukeboxAudioTrack(meter)
        self._video = JukeboxVideoTrack(
            self.s.video_width,
            self.s.video_height,
            self.s.video_fps,
            idle_image=self.s.idle_image,
            meter=meter,
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
        self._play_started = 0.0
        if self._audio is not None:
            self._audio.clear_source()
        if self._video is not None:
            self._video.clear_source()
            self._video.hide_visualizer()

    async def _media_loop(self) -> None:
        while True:
            try:
                await self._play_next()
            except _MEDIA_ERRORS:
                logger.exception("media item failed; skipping")
                self._set_idle()
                await asyncio.sleep(0.2)  # don't hot-loop if an item fails instantly

    async def _play_next(self) -> None:
        self._skip.clear()
        self._seek.clear()  # a seek only applies to the item it was issued for
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
        item.duration = item.duration or resolved.duration
        await self._play_resolved(
            Resolved(resolved.media_url, item.title, duration=item.duration),
            interruptible=False,
        )

    async def _with_skip[T](self, work: asyncio.Future[T], max_wait: float) -> bool:
        """Wait for a worker-thread future without blocking the loop: a .skip or a
        timeout abandons it (the thread can't be cancelled, so it drains in the
        background). True if it finished, False if it was abandoned."""
        skip = asyncio.ensure_future(self._skip.wait())
        racers = cast("set[asyncio.Future[object]]", {work, skip})
        try:
            done, _ = await asyncio.wait(
                racers, timeout=max_wait, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            skip.cancel()
        if work not in done:
            work.add_done_callback(_ignore_result)
            return False
        return True

    async def _resolve(self, url: str) -> Resolved | None:
        work = asyncio.ensure_future(
            asyncio.to_thread(resolve, url, self.s.ytdlp_cookies)
        )
        if not await self._with_skip(work, _RESOLVE_TIMEOUT):
            logger.warning("resolve abandoned for %s (skipped or timed out)", url)
            return None
        try:
            return work.result()
        except (DownloadError, KeyError, OSError) as e:
            logger.warning("resolve failed for %s: %s", url, e)
            return None

    async def _buffer(self, url: str) -> str | None:
        """Pull a remote source to a local temp file so seeks are fast and stay
        A/V-synced: a complete local container seeks cleanly, whereas re-fetching
        a remote stream from the cut point stalls and drifts. -c copy means no
        re-encode, so no CPU spike. Returns the local path, or None on failure."""
        fd, path = tempfile.mkstemp(suffix=".mkv")
        os.close(fd)
        logger.info("buffering %s for smooth seeking", url)
        keep = False  # hand the temp file off to the caller only on success
        try:
            work = asyncio.ensure_future(asyncio.to_thread(_download, url, path))
            if await self._with_skip(work, _BUFFER_TIMEOUT) and work.result():
                keep = True
                return path
            logger.warning("buffering failed/abandoned for %s", url)
            return None
        finally:
            if not keep:
                with contextlib.suppress(OSError):
                    os.unlink(path)

    async def _play_resolved(self, resolved: Resolved, *, interruptible: bool) -> None:
        offset = 0.0
        announced = False
        buffered = False
        buffer_path: str | None = None
        try:
            while True:
                opened = _open_source(resolved, offset)
                if opened is None:
                    self._set_idle()
                    return
                source, ffmpeg = opened
                at = f" @ {offset:.0f}s" if offset else ""
                logger.info("now playing: %s%s", resolved.title, at)
                try:
                    if self._audio is not None and source.audio is not None:
                        self._audio.set_source(source.audio)
                    if self._video is not None and source.video is not None:
                        self._video.set_source(source.video)
                        self._video.hide_visualizer()
                    elif self._video is not None:
                        self._video.clear_source()
                        self._video.show_visualizer()
                    # Best-effort: a reconnect can leave the writer briefly closed,
                    # and a failed state ping must not abort playback.
                    with contextlib.suppress(RuntimeError):
                        self._send({"type": "video", "state": "on"})
                    self._play_offset = offset
                    self._play_started = time.monotonic()
                    # Announce a fresh user-track start once. Fallback episodes
                    # (interruptible) and seek re-opens stay quiet; best-effort
                    # because a reconnect can briefly close the writer.
                    if not announced and not interruptible and self.on_track_change:
                        announced = True
                        with contextlib.suppress(RuntimeError):
                            self.on_track_change()
                    reason = await self._await_end(source, interruptible=interruptible)
                finally:
                    _stop_player(source)
                    if ffmpeg is not None:
                        _stop_ffmpeg(ffmpeg)
                if reason != "seek":
                    self._set_idle()
                    return
                offset = self._seek_target
                # On the first seek of a remote user item, pull it to local disk
                # so this and every later seek are fast and stay A/V-synced (the
                # tile freezes for that one-time download); Jellyfin already seeks
                # server-side via seek_url.
                if resolved.seek_url is None and not buffered:
                    buffered = True
                    buffer_path = await self._buffer(resolved.media_url)
                    if buffer_path is not None:
                        resolved = Resolved(
                            buffer_path, resolved.title, duration=resolved.duration
                        )
        finally:
            if buffer_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(buffer_path)

    async def _await_end(self, source: MediaPlayer, *, interruptible: bool) -> str:
        track = source.video or source.audio
        while track is not None and track.readyState == "live":
            if self._seek.is_set():
                self._seek.clear()
                return "seek"
            if self._skip.is_set():
                return "skip"
            # A queued user request, or a .show change/off, preempts the
            # fallback show immediately (user items aren't interruptible).
            if interruptible and (
                self.playlist.upcoming() or self._fallback_reload.is_set()
            ):
                return "preempt"
            if self._stalled():
                logger.warning("source stalled for %.0fs; skipping", _STALL_TIMEOUT)
                return "stalled"
            await asyncio.sleep(0.5)
        return "ended"

    def _stalled(self) -> bool:
        if self._audio is None or self._video is None:
            return False
        latest = max(self._audio.last_frame_at, self._video.last_frame_at)
        return latest > 0 and time.monotonic() - latest > _STALL_TIMEOUT


def _ignore_result[T](task: asyncio.Future[T]) -> None:
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


def _open_source(
    resolved: Resolved, offset: float
) -> tuple[MediaPlayer, subprocess.Popen[bytes] | None] | None:
    """Open the source positioned at `offset`. A seek always feeds the
    MediaPlayer through an ffmpeg remux that re-bases timestamps and forces
    in-order A/V interleaving, so neither a server transcode (Jellyfin — which
    delivers audio ahead of the subtitle-burned, re-encoded video) nor a
    range-served file drifts out of sync. Jellyfin positions server-side via
    seek_url, so ffmpeg only remuxes; everything else gets an ffmpeg input seek."""
    if offset <= 0:
        player = _open_player(resolved.media_url)
        return (player, None) if player is not None else None
    if resolved.seek_url is not None:
        url, input_seek = resolved.seek_url(offset), 0.0
    else:
        url, input_seek = resolved.media_url, offset
    try:
        ffmpeg = subprocess.Popen(
            _ffmpeg_seek_cmd(url, input_seek), stdout=subprocess.PIPE
        )
    except OSError as e:
        logger.warning("ffmpeg seek failed to start: %s", e)
        return None
    player = _open_player(ffmpeg.stdout, fmt="matroska")
    if player is None:
        _stop_ffmpeg(ffmpeg)
        return None
    return (player, ffmpeg)


def _open_player(source: object, *, fmt: str | None = None) -> MediaPlayer | None:
    # av.FFmpegError: skip an expired/403 source URL instead of crashing the loop.
    try:
        return MediaPlayer(source, format=fmt)
    except (OSError, ValueError, av.FFmpegError) as e:
        logger.warning("open failed: %s", e)
        return None


def _ffmpeg_seek_cmd(url: str, offset: float) -> list[str]:
    # -ss before -i is an input seek (skipped at offset 0, when the server already
    # positioned the stream); -c copy avoids a re-encode; rw_timeout bounds a
    # stalled network read. genpts + avoid_negative_ts re-base timestamps to zero,
    # and max_interleave_delta 0 makes the muxer hold packets until it can write
    # A/V in order — a server transcode can emit audio well ahead of its video,
    # which otherwise drifts them apart.
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-fflags",
        "+genpts",
        "-rw_timeout",
        "20000000",
    ]
    if offset > 0:
        cmd += ["-ss", f"{offset:.3f}"]
    cmd += [
        "-i",
        url,
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-max_interleave_delta",
        "0",
        "-f",
        "matroska",
        "pipe:1",
    ]
    return cmd


def _download(url: str, path: str) -> bool:
    """Copy a remote source into a local container with no re-encode so it can be
    seeked locally; rw_timeout bounds a stalled fetch."""
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-rw_timeout",
        "30000000",
        "-i",
        url,
        "-c",
        "copy",
        "-y",
        path,
    ]
    try:
        proc = subprocess.run(cmd, timeout=_BUFFER_TIMEOUT, capture_output=True)
    except _DOWNLOAD_ERRORS:
        return False
    return proc.returncode == 0 and os.path.getsize(path) > 0


def _stop_ffmpeg(proc: subprocess.Popen[bytes]) -> None:
    proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=2)
