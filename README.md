# obby-jukebox

A community video jukebox for IRC. Queue links (anything `yt-dlp` supports) and they play, back to back, as a live video stream you can watch together in your IRC client.

Live on **irc.h4ks.com** — join **$tv**.

## Use it

Queue and control it from the `$tv` channel (commands are ignored in PMs):

```
.play <url>     add a video to the queue
.queue          show what's coming up
.now            what's playing now
.skip           skip the current video
.clear          clear the queue
.help           list the commands
```

When the queue is empty the bot plays a fallback show off Jellyfin instead of an idle
card. Admins (IRC accounts in `ADMIN_ACCOUNTS`) control it:

```
.show <name> [SxxExx]   play this series from a season/episode (default S01E01)
.show search <name>     search Jellyfin for matching series
.show off               stop the fallback (back to the idle card)
```

## Run your own

```sh
docker build -t obby-jukebox .
docker run --rm \
  -e IRC_HOST=irc.example.com \
  -e IRC_NICK=jukebox \
  -e IRC_SASL_USER=jukebox -e IRC_SASL_PASS=... \
  -e VOICE_CHANNEL='$tv' \
  -e JELLYFIN_URL=http://jellyfin:8096 -e JELLYFIN_API_KEY=... \
  -e ADMIN_ACCOUNTS=you \
  -p 8080:8080 \
  obby-jukebox
```

The bot connects to the IRC server, joins a voice/stream channel, and publishes the
current queue item as a WebRTC video stream. The REST API on `:8080` manages the queue
(`POST /queue`, `GET /queue`, `GET /now`, `POST /skip`, `POST /clear`).

Requires an IRC server that supports WebRTC voice/stream channels (the `obsidianirc/voice`
capability) and a watching client such as [ObsidianIRC](https://github.com/obbyworld/ObsidianIRC).

## Develop

```sh
uv sync
uv run pytest
uv run ruff check .
uv run mypy
```

## License

MIT
