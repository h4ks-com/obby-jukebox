# obby-jukebox — plan

A tiny, reusable homelab microservice: an IRC bot that is also a WebRTC **video publisher**,
fed by a **yt-dlp queue**, controlled by **CloudBot over HTTP**. It publishes to one constant
`$` stream channel on the h4ks ObbyIRCd, so anyone in ObsidianIRC can watch a shared video feed.

- Repo: `h4ks-com/obby-jukebox` · image `ghcr.io/h4ks-com/obby-jukebox:latest`
- Runs in homelab (k3s), namespace `obby-jukebox`
- Constant channel: `$jukebox`

## Decisions (locked)
- **Standalone external bot** — separate service, real IRC client + real WebRTC peer (not the in-process orca path).
- **Python + aiortc** for WebRTC. (pion is only the *server's* SFU library — not ours. Our client uses **aiortc** and connects to **coturn.h4ks.com** over standard TURN; same protocol.)
- **Homelab** hosting — publishes to the h4ks SFU over the internet via coturn. The homelab→SFU media path is the #1 risk and is validated at Milestone 3.
- **Minimal jukebox** API for v1: play / queue / skip / now-playing / clear; one constant channel.
- Libraries: all permissive, maintained — **aiortc** (BSD-3), **yt-dlp** (Unlicense), **PyAV** (BSD-3), **FastAPI**+**uvicorn** (MIT/BSD), **irctokens** (ISC, IRCv3 tokenizer), **pydantic-settings** (MIT).

---

## How obby voice/video actually works (researched 2026-06; source of truth for the handshake)

Three parts:
- **ObbyIRCd** `src/modules/voice-channels.c` — a *dumb signaling relay*. Gates JOIN on CAP
  `obsidianirc/voice`; shovels the `+obsidianirc/rtc` TAGMSG payload to/from the SFU over a unix
  socket (`/run/obbyirc/voice.sock`); mints TURN creds. Never parses SDP — **media-agnostic** (no
  audio/video distinction). Channel prefixes: `^` = voice (all publish), `$` = stream
  (streamer/viewer split, ≤4 streamers). No channel mode letter.
- **obby-api** (`hosted-backend`, Go) — a hand-rolled **pion v4 SFU** + TURN. `RegisterDefaultCodecs()`
  ⇒ full **VP8/VP9/H264** + Opus, with PLI/FIR keyframe forwarding and dual camera+screen tracks.
  The signaling-envelope JSON schema (inside `payload`) is defined here, not in the IRCd.
- **ObsidianIRC** — browser client; all voice/video in one file `src/lib/voice.ts` (the reference impl).

**Already-exists note:** `hosted-backend/orca/video_player.go` already streams VP8 into a channel
*in-process* (`ffmpeg → VP8(IVF)+Opus → RFC-7741 RTP → peer.SendVideoRTP()` via
`voiceManager.RegisterLocal`). We deliberately chose the standalone external path instead so the
service is decoupled + reusable; orca's ffmpeg flags + VP8 packetization are a useful reference.

### Handshake 1 — IRC (TLS `irc.h4ks.com:6697`, same server CloudBot uses)
`CAP LS 302` → `CAP REQ :message-tags server-time obsidianirc/voice` → SASL `AUTHENTICATE PLAIN`
(bot account; **TURN creds are minted per IRC account**) → `CAP END` → `JOIN $jukebox` → wait for
the self-echo `JOIN`. (Alternative: wear bot mode `+B` to bypass the voice-cap join gate — but we
still need `message-tags`. Using the cap is cleaner.)

### Handshake 2 — WebRTC over IRC (all `@+obsidianirc/rtc=<irc-escaped-json> TAGMSG $jukebox`)
1. → `{"type":"join","channel":"$jukebox"}`
2. ← `{"type":"joined","turn":{"urls":[…],"username":…,"password":…},"members":[…]}`
   — build `RTCPeerConnection(iceServers=[google STUN, this TURN])`, `addTrack(video)`, `addTrack(audio)`
3. → `{"type":"offer","sdp":…}` — aiortc gathers ICE into the SDP (non-trickle); the SDP is large, so
   **CHUNK** it: split into N TAGMSGs sharing an `id`, each with `seq`/`total`, keeping every escaped
   line under ~7800 bytes (server tag limit 8191). Reassemble inbound chunks the same way.
4. ← `{"type":"answer","sdp":…,"tracks":[{track_id,mid?,member,kind}…]}` → `setRemoteDescription`;
   handle any trickled `{"type":"ice","cand":…,"mid":…,"mlineidx":N}` from the SFU → `addIceCandidate`.
5. → `{"type":"video","state":"on"}`; answer the SFU's server-initiated renegotiation `offer`s.
6. Media (DTLS-SRTP) flows aiortc ↔ pion-SFU, relayed through coturn. The IRCd is out of the media path.

Envelope `type` vocabulary (from voice.ts): `join leave joined offer answer ice presence error mic
video speaking silent deaf screen hand react promote demote role`. IRCv3 tag escaping: `;`→`\:`,
space→`\s`, `\`→`\\`, CR→`\r`, LF→`\n`.

### h4ks facts
- obbyircd = `irc.h4ks.com:6697` (TLS) / `obby.h4ks.com` (WSS), runs via obby-stack compose on Coolify.
- TURN = `coturn.h4ks.com:3478/5349`, HMAC `use-auth-secret`; the IRCd rewrites the `joined` turn field
  with fresh per-account creds. Our client just uses whatever `turn` the `joined` message gives it.
- SFU media UDP `50000-50100` IS published by obby-stack compose (what h4ks runs). A homelab publisher
  behind NAT still relays via coturn. ⚠️ Documented failure mode: if media can't reach the SFU PC and
  TURN relay isn't solid, "video silently fails (audio survives via TURN relay)" — validate at M3.

---

## Service design

```
src/obby_jukebox/
  __main__.py     entrypoint: wire IRC + signaling + publisher + player + api, run asyncio loop
  config.py       env-var config (pydantic-settings)
  ircconn.py      asyncio TLS IRC client (irctokens): CAP/SASL, JOIN, TAGMSG w/ tags, escape/unescape
  signaling.py    +obsidianirc/rtc envelope codec + SDP chunk/reassemble + join→offer→answer→ice SM
  publisher.py    aiortc RTCPeerConnection (TURN from joined + STUN), persistent VP8+Opus tracks
  player.py       queue + yt-dlp resolve + PyAV decode → frames; auto-advance; idle placeholder
  api.py          FastAPI control surface
assets/idle.png   "nothing playing" placeholder
tests/
Dockerfile  pyproject.toml  compose.yaml(local)  README.md  PLAN.md
```
k8s manifests live in the **homelab** repo at `k8s/base/obby-jukebox/` (like video-mcp), not the app repo.

### Media pipeline — persistent tracks, no per-item renegotiation
One VP8 + one Opus track exist for the whole session, fed by a queue-aware source:
```
yt-dlp (resolve URL → direct stream URL, using the mounted cookies)
  → PyAV/ffmpeg decode current item → custom aiortc Video/AudioStreamTrack yields frames
  → on item EOF: pop next from queue; on empty: idle placeholder (logo frame @ low fps + silence)
  → aiortc encodes VP8 (~30fps) + Opus 48k → RTP → PC
```
Swapping queue items just changes the frame source — the PC + tracks persist, so viewers never see a
reconnect between items.

### HTTP API (minimal v1)
`POST /queue {url}` · `GET /queue` · `GET /now` · `POST /skip` · `POST /clear` · `GET /healthz`.
Gate with a static `API_KEY` header (or Kong key-auth at the edge, like video-mcp).

### CloudBot plugin (separate, in CloudBot)
`.vplay <url>` → POST /queue · `.vqueue` → GET /queue · `.vskip` · `.vnp` → GET /now · `.vclear`.
Config: the jukebox base URL + API key in CloudBot config.

---

## Env vars (12-factor; secrets via k8s Secret)
```
IRC_HOST=irc.h4ks.com
IRC_PORT=6697
IRC_TLS=true
IRC_NICK=jukebox
IRC_SASL_USER=jukebox
IRC_SASL_PASS=***            # secret
VOICE_CHANNEL=$jukebox
STUN_URLS=stun:stun.l.google.com:19302   # joined.turn is added on top
ICE_TRANSPORT_POLICY=all     # set "relay" to force TURN if homelab host/srflx candidates fail
YTDLP_COOKIES=/cookies/cookies.txt       # reflected h4kstream-ytdlp-cookies secret
VIDEO_WIDTH=1280
VIDEO_HEIGHT=720
VIDEO_FPS=30
VIDEO_BITRATE=2500k
IDLE_IMAGE=/app/assets/idle.png
MAX_QUEUE=100
MAX_ITEM_SECONDS=0           # 0 = no cap
HTTP_BIND=0.0.0.0
HTTP_PORT=8080
API_KEY=***                  # secret (or rely on Kong at edge)
LOG_LEVEL=info
```

## Homelab deploy (k3s, matches h4kstream / video-mcp)
- Image `ghcr.io/h4ks-com/obby-jukebox:latest`, CI builds on push, **keel** auto-deploys
  (`keel.sh/policy: force`, poll 5m).
- Namespace `obby-jukebox`; single replica (in-memory queue); `strategy: Recreate`.
- `/healthz` liveness+readiness.
- Mount the **reflected** `h4kstream-ytdlp-cookies` secret at `/cookies` (optional: true so the pod
  starts before reflection lands). **Prereq:** add `obby-jukebox` to the source secret's
  `reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces`. **No new cookie cron** — reuse the
  existing h4kstream `ytdlp-cookie-refresher` CronJob.
- VP8 encode is CPU-heavy: start on CPU; if it can't hold ~30fps@720p, schedule onto the GPU node
  (the `gpu: "true"` node, AMD 780M `/dev/dri`, same one video-mcp uses) and encode via VAAPI.

---

## Milestones
1. **IRC handshake** — asyncio TLS connect, CAP/SASL, `JOIN $jukebox`, see self-echo.
2. **WebRTC connect** — join/joined/offer/answer/ice over TAGMSG + SDP chunking; publish a test tone +
   black frame; confirm `connectionState=connected`.
3. **▶ media validation (top risk)** — real ffmpeg VP8; open ObsidianIRC as a viewer on `$jukebox`,
   confirm video survives homelab→coturn→SFU. If it silently fails, try `ICE_TRANSPORT_POLICY=relay`,
   then fall back to co-locating the service on h4ks.
4. **yt-dlp + queue** — resolve (with cookies), transcode, auto-advance, idle placeholder.
5. **API + CloudBot plugin.**
6. **Dockerize + k8s manifests (homelab repo) + create `h4ks-com/obby-jukebox` + deploy.**

## Risks / watch-items
- Media path homelab→SFU via coturn (the "silent video fail" mode) — M3; relay-policy or co-locate as fallback.
- aiortc non-trickle ICE vs the SFU — validate the SFU accepts SDP-embedded candidates at M2.
- SDP chunking correctness (video SDP > 8191-byte tag limit).
- VP8 encode CPU on homelab — tune res/fps or use the GPU node.

## Prereqs (before M3)
- Register a bot account on h4ks (e.g. `jukebox`) with a SASL password → k8s Secret.
- Pick/create the constant channel (`$jukebox`).
- Confirm `coturn.h4ks.com:3478` reachable from homelab (it's public).
- Allow `obby-jukebox` namespace in the h4kstream cookie secret's Reflector annotation.

## References
- ObbyIRCd voice: `~/projects/ObbyIRCd/src/modules/voice-channels.c`
- SFU + the existing in-process player: `~/projects/hosted-backend/voice.go`, `voice_bridge.go`,
  `voice_local.go`, `orca/video_player.go`
- Client reference impl: `~/projects/ObsidianIRC/src/lib/voice.ts`
- Cookie-refresher + reflected-secret pattern: `~/projects/homelab/k8s/base/h4kstream-cookie-refresher/`,
  `~/projects/homelab/k8s/base/video-mcp/deployment.yaml`
- Structural template (packaging): `~/projects/video-creator-mcp`
