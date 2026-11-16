"""Configuration loading and validation.

Non-secret settings live in ``config.json`` while every secret (the Discord
token, API keys, OAuth client secrets) is read from environment variables /
``.env`` so nothing sensitive is ever committed.

The loader merges the two sources, validates the result and exposes a fully
typed :class:`Config` object to the rest of the application.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from server_settings import validate_server
from app_paths import SECRETS


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid."""


# A sensible, fully-populated template written by ``/createconfig`` and used as
# the default when generating ``config.json``.
DEFAULT_CONFIG: dict[str, Any] = {
    "poll_interval": 60,
    "embed_color": "0xFF0000",
    "custom_message": "**{name}** just went live on {platform}!",
    "presence_idle": "for live streams...",
    "ping_once_per_stream": True,
    "delete_after_offline": False,
    "buttons": {
        "watch_stream": {"enabled": True, "label": "Watch Stream", "emoji": "▶️"},
        "join_discord": {"enabled": True, "label": "Join Discord", "emoji": "💬", "url": ""},
        "subscribe": {"enabled": True, "label": "Subscribe", "emoji": "📺", "url": ""},
    },
    "themes": {
        "youtube": "0xFF0000",
        "twitch": "0x9146FF",
        "default": "0xFF0000",
    },
    "servers": [],
}


def _coerce_color(value: Any) -> int:
    """Convert a colour expressed as int / hex-string into an ``int``.

    Args:
        value: ``"0xFF0000"``, ``"#FF0000"``, ``"FF0000"`` or an ``int``.

    Returns:
        The colour as an integer suitable for ``discord.Colour``.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if text.lower().startswith("0x"):
            text = text[2:]
        try:
            return int(text, 16)
        except ValueError as exc:
            raise ConfigError(f"Invalid colour value: {value!r}") from exc
    raise ConfigError(f"Invalid colour value: {value!r}")


@dataclass(slots=True)
class ButtonConfig:
    """A single configurable Discord link button."""

    enabled: bool
    label: str
    emoji: str | None = None
    url: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ButtonConfig":
        return cls(
            enabled=bool(data.get("enabled", True)),
            label=str(data.get("label", "Link")),
            emoji=data.get("emoji"),
            url=data.get("url"),
        )


@dataclass(slots=True)
class ServerConfig:
    """A target Discord server (supports multi-guild delivery)."""

    guild_id: int
    notification_channel_id: int
    live_role_id: int | None = None
    streams: list[dict[str, str]] = field(default_factory=list)
    message_type: str = "embed"
    custom_message: str | None = None
    delete_after_offline: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServerConfig":
        role = data.get("live_role_id") or None
        return cls(
            guild_id=int(data["guild_id"]),
            notification_channel_id=int(data["notification_channel_id"]),
            live_role_id=int(role) if role else None,
            streams=data.get("streams", []),
            message_type=data.get("message_type", "embed"),
            custom_message=data.get("custom_message"),
            delete_after_offline=data.get("delete_after_offline"),
        )


@dataclass(slots=True)
class Config:
    """Fully typed application configuration."""

    # --- Secrets (sourced from the environment / .env) ---------------------
    discord_token: str
    youtube_api_key: str | None
    twitch_client_id: str | None
    twitch_client_secret: str | None

    # --- General ----------------------------------------------------------
    streamer_name: str
    stream_paused: bool
    discord_user_id: str
    message_type: str
    poll_interval: int
    embed_color: int
    custom_message: str
    presence_idle: str
    ping_once_per_stream: bool
    delete_after_offline: bool

    # --- Delivery ---------------------------------------------------------
    servers: list[ServerConfig]

    # --- Platforms (raw values; consumed by the platform classes) ---------
    youtube_channel_id: str | None
    twitch_username: str | None

    # --- Presentation -----------------------------------------------------
    buttons: dict[str, ButtonConfig]
    themes: dict[str, int]

    # Original parsed JSON, kept for diagnostics / future platform configs.
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def theme_for(self, platform: str) -> int:
        """Return the embed colour for ``platform`` falling back to default."""
        if platform in self.themes:
            return self.themes[platform]
        return self.themes.get("default", self.embed_color)


def _load_json(path: Path, *, allow_missing: bool = False) -> dict[str, Any]:
    """Read and parse ``config.json`` merged over the defaults."""
    if not path.exists():
        if allow_missing:
            return json.loads(json.dumps(DEFAULT_CONFIG))
        raise ConfigError(f"Config file not found at '{path}'.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config.json is not valid JSON: {exc}") from exc

    merged = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy of defaults
    if not isinstance(data, dict):
        raise ConfigError("config.json must be an object")
    legacy = {"guild_id", "notification_channel_id", "live_role_id", "streamer_name", "youtube_channel_id", "twitch_username", "discord_token"}
    if legacy.intersection(data):
        raise ConfigError("Old top-level stream settings are unsupported; configure servers instead.")
    if data.get("webhook_mode"):
        raise ConfigError("Webhook delivery was removed; configure a Discord notification channel.")
    merged.update(data)
    try:
        if not isinstance(merged["servers"], list):
            raise ValueError("servers must be a list")
        for server in merged["servers"]:
            validate_server(server)
        ids = [server["guild_id"] for server in merged["servers"]]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate guild_id")
    except (TypeError, ValueError, AttributeError) as exc:
        raise ConfigError(f"Invalid server configuration: {exc}") from exc
    return merged


def load_config(
    config_path: str | os.PathLike[str] = "config.json",
    env_path: str | os.PathLike[str] = ".env",
    *,
    allow_missing: bool = False,
) -> Config:
    """Load, merge and validate configuration.

    Args:
        config_path: Path to the JSON configuration file.
        env_path: Path to the ``.env`` file holding secrets.

    Returns:
        A validated :class:`Config`.

    Raises:
        ConfigError: If required values are missing or malformed.
    """
    load_dotenv(env_path)
    data = _load_json(Path(config_path), allow_missing=allow_missing)

    # The owner can replace secrets through the local panel.
    secrets_path = SECRETS if os.environ.get("APP_DATA_DIR") else Path(env_path).resolve().parent / ".secrets.json"
    try:
        saved_secrets = json.loads(secrets_path.read_text(encoding="utf-8")) if secrets_path.exists() else {}
        if not isinstance(saved_secrets, dict):
            raise ValueError("Expected an object")
    except (ValueError, OSError) as exc:
        raise ConfigError(f"Cannot read saved secrets: {exc}") from exc
    def secret(name: str) -> str:
        return saved_secrets.get(name) or os.getenv(name, "")

    discord_token = secret("DISCORD_TOKEN")
    if not discord_token:
        raise ConfigError(
            "Discord token missing. Set DISCORD_TOKEN in your .env file."
        )

    # Buttons -> typed objects, merged over defaults.
    button_data: dict[str, Any] = dict(DEFAULT_CONFIG["buttons"])
    button_data.update(data.get("buttons") or {})
    buttons = {key: ButtonConfig.from_dict(val) for key, val in button_data.items()}

    # Themes -> ints.
    theme_data: dict[str, Any] = dict(DEFAULT_CONFIG["themes"])
    theme_data.update(data.get("themes") or {})
    themes = {key: _coerce_color(val) for key, val in theme_data.items()}

    servers = [ServerConfig.from_dict(server) for server in data["servers"]]

    poll_interval = int(data.get("poll_interval", 60))
    if poll_interval < 10:
        raise ConfigError("poll_interval must be at least 10 seconds.")

    config = Config(
        discord_token=discord_token,
        youtube_api_key=secret("YOUTUBE_API_KEY") or None,
        twitch_client_id=secret("TWITCH_CLIENT_ID") or None,
        twitch_client_secret=secret("TWITCH_CLIENT_SECRET") or None,
        streamer_name="Streamer",
        stream_paused=False,
        discord_user_id="",
        message_type="embed",
        poll_interval=poll_interval,
        embed_color=_coerce_color(data.get("embed_color", "0xFF0000")),
        custom_message=str(data.get("custom_message") or "**{name}** is live on {platform}!"),
        presence_idle=str(data.get("presence_idle") or "for live streams..."),
        ping_once_per_stream=bool(data.get("ping_once_per_stream", True)),
        delete_after_offline=bool(data.get("delete_after_offline", False)),
        servers=servers,
        youtube_channel_id=None,
        twitch_username=None,
        buttons=buttons,
        themes=themes,
        raw=data,
    )

    _validate(config)
    return config


def _validate(config: Config) -> None:
    """Validate cross-field invariants, raising :class:`ConfigError`."""
    if any(stream.get("twitch_username") for server in config.servers for stream in server.streams) and not (config.twitch_client_id and config.twitch_client_secret):
        raise ConfigError("Twitch is configured but TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are missing from the environment.")
