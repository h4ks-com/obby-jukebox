# obby-jukebox

A community video jukebox for IRC. Queue links (anything `yt-dlp` supports) and they
play back to back as a live video stream your IRC client can watch together.

Try it live: connect to `irc.h4ks.com` and join `$tv`.

## Commands

In the stream channel (ignored in PMs):

```
.yt <terms>   search YouTube; lists 3 results
.play <url>   queue a video (anything yt-dlp supports)
.play <n>     queue result n from your last .yt (or .play for the top one)
.seek <time>  jump within the current video — 90, 1:30, or 1:30:00
.queue        what's coming up
.now          what's playing
.skip         skip the current video
.clear        empty the queue
.help         list commands
```

`.yt` results are remembered per user, so search, then `.play <n>` the one you want.

When the queue is empty the bot can play a Jellyfin series or movie as a fallback
channel. Admins — logged-in accounts listed in `ADMIN_ACCOUNTS` — control it:

```
.show <name> [SxxExx]   play a series, optionally from a season/episode
.showsearch <name>      list matching series with their seasons
.movie <name>           play a movie on loop
.moviesearch <name>     list matching movies
.show off               stop the fallback
```

## Run

```sh
docker build -t obby-jukebox .
docker run --rm \
  -e IRC_HOST=irc.example.com -e IRC_NICK=jukebox \
  -e IRC_SASL_USER=jukebox -e IRC_SASL_PASS=secret \
  -e VOICE_CHANNEL='$stream' \
  -e ADMIN_ACCOUNTS=alice,bob \
  -p 8080:8080 \
  obby-jukebox
```

See `.env.example` for every setting (it doubles as a `.env` template). The bot
logs in over SASL, registering the account on the way in if the server supports
IRCv3 account-registration and it doesn't exist yet (set `IRC_REGISTER_EMAIL` if
the server requires an address). The fallback channel is optional: set
`JELLYFIN_URL` and `JELLYFIN_API_KEY` to enable it, and the `.show` commands
disable themselves cleanly when they're unset.

It needs an IRC server with WebRTC stream channels (the `obsidianirc/voice`
capability) and a client such as [Obby](https://github.com/obbyworld/obby) to
watch. Stream channels are `$`-prefixed (e.g. `$tv`), not regular `#` channels.
The REST API on `:8080` mirrors the queue: `POST /queue`, `GET /queue`,
`GET /now`, `POST /skip`, `POST /seek`, `POST /clear`.

## Develop

```sh
uv sync
uv run pytest
uv run ruff check .
uv run mypy
```

## License

MIT
