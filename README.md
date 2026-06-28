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

## Run your own

```sh
docker build -t obby-jukebox .
docker run --rm \
  -e IRC_HOST=irc.example.com \
  -e IRC_NICK=jukebox \
  -e IRC_SASL_USER=jukebox -e IRC_SASL_PASS=... \
  -e VOICE_CHANNEL='$tv' \
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
