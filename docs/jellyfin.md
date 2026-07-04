# Jellyfin

The fallback channel (`.show` / `.movie`) streams from Jellyfin, which transcodes
each title to a size-capped h264 stream for the bot. Configure two things on the
Jellyfin server for it to look right (Dashboard → Playback → Transcoding):

- **Hardware acceleration** — enable a type your GPU supports (e.g. VAAPI). The
  bot always transcodes rather than direct-playing, so this keeps a 4K/HEVC
  source off the CPU and lets tone mapping run on the GPU.
- **Tone mapping** — without it, HDR titles (10-bit BT.2020/PQ, including Dolby
  Vision and HDR10+) transcode to SDR washed out: grey, low-contrast,
  desaturated. SDR titles are unaffected. Enable the main **tone mapping**
  option. For Dolby Vision / HDR10+ that is "Enable tone mapping" (the
  Vulkan/libplacebo path), *not* "Enable VPP tone mapping" (VAAPI VPP), which
  does not affect those sources.
