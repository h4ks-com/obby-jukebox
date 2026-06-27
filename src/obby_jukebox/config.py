"""Environment-driven settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    irc_host: str = "irc.h4ks.com"
    irc_port: int = 6697
    irc_tls: bool = True
    irc_nick: str = "jukebox"
    irc_sasl_user: str = ""
    irc_sasl_pass: str = ""

    voice_channel: str = "$youtube"

    stun_url: str = "stun:coturn.h4ks.com:3478"

    ytdlp_cookies: str = ""  # path to a cookies.txt, optional

    video_width: int = 1280
    video_height: int = 720
    video_fps: int = 30
    idle_image: str = ""  # path to a placeholder image; blank → generated

    max_queue: int = 100

    http_bind: str = "0.0.0.0"
    http_port: int = 8080
    api_key: str = ""  # blank → API is unauthenticated (gate at the edge instead)

    log_level: str = "info"
