"""The core monitoring cog.

Polls every configured platform on a fixed interval, applies duplicate /
offline detection and dispatches notifications via Discord bot messages. It also keeps the bot presence and statistics up to date.
"""

from __future__ import annotations

import asyncio
import sqlite3
import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands, tasks

from app_paths import CONFIG, STATUS
from platforms import build_platforms
from platforms.base import BasePlatform, StreamInfo
from utils.config import Config, ConfigError, load_config
from utils.embeds import StreamButtons, build_live_embed, build_message_content
from utils.logger import get_logger

log = get_logger("monitor")


def error_code(exc: Exception, fallback: str) -> str:
    status = getattr(exc, 'status', None) or getattr(exc.__cause__, 'status', None)
    if status in (401, 403):
        return 'access_denied'
    if status == 429:
        return 'rate_limited'
    return fallback


def text_marker(marker: str) -> str:
    """Invisible delivery marker retained in plain messages for recovery."""
    bits = ''.join(f'{byte:08b}' for byte in marker.encode('ascii'))
    return '\u2063' + bits.replace('0', '\u200b').replace('1', '\u200c') + '\u2063'


@dataclass(slots=True)
class SentMessage:
    """Reference to a delivered notification (for later edit/delete)."""

    guild_id: int
    channel_id: int
    message_id: int


@dataclass(slots=True)
class StreamState:
    """Per-platform runtime state used for de-duplication."""

    is_live: bool = False
    current_key: str | None = None
    notified_key: str | None = None
    messages: list[SentMessage] = field(default_factory=list)
    last_info: StreamInfo | None = None
    checked_at: datetime | None = None
    check_failed: bool = False
    check_error: str | None = None
    delivery_error: str | None = None
    delete_error: str | None = None


@dataclass(slots=True)
class Stats:
    """Aggregate statistics surfaced by the /status and /platforms commands."""

    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    notifications_sent: int = 0
    checks_run: int = 0
    last_check: datetime | None = None
    per_platform: dict[str, int] = field(default_factory=dict)


class StreamMonitor(commands.Cog):
    """Background monitor that watches every configured platform."""

    def __init__(self, bot: commands.Bot, config: Config) -> None:
        self.bot = bot
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self.platforms: list[BasePlatform] = []
        self.state: dict[BasePlatform, StreamState] = {}
        self.stats = Stats()
        self.config_lock = asyncio.Lock()
        self.rejected_token = None
        self.runtime_error: str | None = None
        # Configure the polling interval before the loop starts.
        self.monitor_loop.change_interval(seconds=config.poll_interval)

    # -- lifecycle ---------------------------------------------------------
    async def cog_load(self) -> None:
        """Create the HTTP session and build platform instances."""
        timeout = aiohttp.ClientTimeout(total=20)
        self.session = aiohttp.ClientSession(timeout=timeout)
        await self.apply_config(self.config)

    @staticmethod
    def platform_key(platform: BasePlatform) -> tuple[int, str, str]:
        config = platform.config
        channel = config.youtube_channel_id if platform.name == "youtube" else (config.twitch_username or "").lower()
        return (config.servers[0].guild_id, platform.name, channel or "")

    async def apply_config(self, config: Config) -> None:
        """Swap settings between checks, retaining state for unchanged streams."""
        assert self.session is not None
        async with self.config_lock:
            old = {self.platform_key(platform): self.state[platform] for platform in self.platforms}
            platforms = []
            for server in config.servers:
                for stream in server.streams:
                    stream_config = replace(config, servers=[server], streamer_name=stream["name"],
                                            stream_paused=stream.get("paused", False),
                                            discord_user_id=stream.get("discord_user_id", ""),
                                            youtube_channel_id=stream.get("youtube_channel_id") or None,
                                            twitch_username=stream.get("twitch_username") or None,
                                            message_type=server.message_type,
                                            custom_message=server.custom_message or config.custom_message,
                                            delete_after_offline=server.delete_after_offline if server.delete_after_offline is not None else config.delete_after_offline)
                    platforms.extend(build_platforms(stream_config, self.session))
            self.platforms = platforms
            self.state = {platform: old.get(self.platform_key(platform), StreamState()) for platform in platforms}
            self.config = config
            self.bot.config = config
            commands_cog = self.bot.get_cog("AdminCommands") if hasattr(self.bot, "get_cog") else None
            if commands_cog:
                commands_cog.config = config
            self.monitor_loop.change_interval(seconds=config.poll_interval)
            for platform in platforms:
                self.stats.per_platform.setdefault(platform.name, 0)

    @tasks.loop(seconds=3)
    async def reload_loop(self) -> None:
        try:
            config = load_config(CONFIG, allow_missing=not self.config.servers)
            self.runtime_error = None
            if config.discord_token != self.config.discord_token:
                if config.discord_token != self.rejected_token and self.bot.user:
                    try:
                        async with self.session.get(
                            "https://discord.com/api/v10/users/@me",
                            headers={"Authorization": "Bot " + config.discord_token},
                        ) as response:
                            if response.status == 200 and (await response.json()).get("id") == str(self.bot.user.id):
                                self.bot.next_config = config
                                asyncio.create_task(self.bot.close())
                                return
                            if response.status in (401, 403, 200):
                                self.rejected_token = config.discord_token
                                self.runtime_error = "discord_token_invalid"
                                log.error("New Discord token is invalid or belongs to a different bot; keeping the current connection.")
                            else:
                                self.runtime_error = "discord_unavailable"
                                log.warning("Discord token validation temporarily unavailable (HTTP %s).", response.status)
                    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                        self.runtime_error = "discord_unavailable"
                        log.warning("Discord token validation temporarily unavailable: %s", type(exc).__name__)
                if config.discord_token == self.rejected_token:
                    self.runtime_error = "discord_token_invalid"
                config = replace(config, discord_token=self.config.discord_token)
            else:
                self.rejected_token = None
            if config != self.config:
                old_ids = {server.guild_id for server in self.config.servers}
                await self.apply_config(config)
                for server in config.servers:
                    if server.guild_id not in old_ids:
                        guild = discord.Object(id=server.guild_id)
                        self.bot.tree.copy_global_to(guild=guild)
                        try:
                            await self.bot.tree.sync(guild=guild)
                        except discord.DiscordException as exc:
                            log.error("Could not sync commands for guild %s: %s", server.guild_id, exc)
                log.info("Configuration reloaded.")
        except (ConfigError, OSError, ValueError, TypeError) as exc:
            self.runtime_error = "config_error"
            log.error("Configuration reload failed; keeping previous settings: %s", exc)


    async def cog_unload(self) -> None:
        """Stop the loop and close the HTTP session cleanly."""
        if self.monitor_loop.is_running():
            self.monitor_loop.cancel()
        if self.reload_loop.is_running():
            self.reload_loop.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    def start(self) -> None:
        """Start the polling loop if it is not already running."""
        if not self.monitor_loop.is_running():
            self.monitor_loop.start()
        if not self.reload_loop.is_running():
            self.reload_loop.start()

    # -- main loop ---------------------------------------------------------
    @tasks.loop(seconds=60)
    async def monitor_loop(self) -> None:
        """Poll every platform once and react to state changes."""
        await self.check_all()

    @monitor_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def check_all(self) -> list[StreamInfo]:
        """Check all platforms a single time and process transitions.

        Returns:
            The list of currently-live streams (useful for /forcecheck).
        """
        async with self.config_lock:
            self.stats.checks_run += 1
            self.stats.last_check = datetime.now(timezone.utc)
            live_now: list[StreamInfo] = []

            for platform in self.platforms:
                try:
                    info = await platform.get_stream_info()
                except Exception as exc:  # defensive: never let one platform break the loop
                    log.error("Error checking %s: %s", platform.display_name, exc)
                    state = self.state[platform]
                    state.checked_at = datetime.now(timezone.utc)
                    state.check_failed = True
                    state.check_error = error_code(exc, "platform_unavailable")
                    if state.is_live and state.last_info:
                        live_now.append(state.last_info)
                    continue

                state = self.state[platform]
                state.checked_at = datetime.now(timezone.utc)
                state.check_failed = False
                state.check_error = None
                if getattr(self.bot, "delivery_store", None):
                    try:
                        await self._recover_expired(platform, state, info.unique_key if info else None)
                    except sqlite3.Error as exc:
                        state.delivery_error = "storage_error"
                        log.error("Delivery state could not be saved: %s", type(exc).__name__)
                if info:
                    live_now.append(info)
                    try:
                        await self._handle_live(platform, state, info)
                    except sqlite3.Error as exc:
                        state.delivery_error = "storage_error"
                        log.error("Delivery state could not be saved; notification deferred: %s", type(exc).__name__)
                else:
                    try:
                        await self._handle_offline(platform, state)
                    except sqlite3.Error as exc:
                        state.delivery_error = "storage_error"
                        log.error("Delivery state could not be saved: %s", type(exc).__name__)

            await self._update_presence(live_now)
            self.write_status()
            return live_now

    def write_status(self) -> None:
        """Publish last observations for the separate local admin process."""
        path = STATUS
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            entries = []
            for platform in self.platforms:
                state = self.state[platform]
                config = platform.config
                entries.append({"guild_id": config.servers[0].guild_id, "name": config.streamer_name,
                                "platform": platform.name, "channel": config.youtube_channel_id if platform.name == "youtube" else config.twitch_username,
                                "status": "unknown" if state.checked_at is None or state.check_failed else ("online" if state.is_live else "offline"),
                                "checked_at": state.checked_at.isoformat() if state.checked_at else None,
                                "check_error": state.check_error,
                                "delivery_error": state.delivery_error, "delete_error": state.delete_error,
                                "title": state.last_info.title if state.is_live and state.last_info else None,
                                "url": state.last_info.url if state.is_live and state.last_info else None})
            data = {"updated_at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), "poll_interval": self.config.poll_interval, "runtime_error": self.runtime_error, "streams": entries}
            fd, name = tempfile.mkstemp(dir=path.parent, prefix=".status-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump(data, file, ensure_ascii=False)
                os.replace(name, path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        except OSError as exc:
            log.error("Could not publish status: %s", type(exc).__name__)

    # -- transitions -------------------------------------------------------
    async def _handle_live(
        self, platform: BasePlatform, state: StreamState, info: StreamInfo
    ) -> None:
        """Handle a platform that is currently live."""
        new_stream = not state.is_live or state.current_key != info.unique_key
        if new_stream:
            log.live("%s %s went live: %s", platform.emoji, info.channel_name, info.title)
            state.is_live = True
            state.current_key = info.unique_key
            if not self.config.ping_once_per_stream:
                state.notified_key = None
        state.last_info = info
        if state.notified_key != info.unique_key:
            if not platform.config.stream_paused and await self._send_notification(state, info, platform.config, platform):
                state.notified_key = info.unique_key
        elif not new_stream and platform.config.message_type == "embed":
            await self._refresh_messages(state, info)

    async def _handle_offline(self, platform: BasePlatform, state: StreamState) -> None:
        """Handle a platform that is offline, resetting state if needed."""
        if state.is_live:
            log.offline("%s %s stream ended.", platform.emoji, platform.display_name)
        if state.messages and platform.config.delete_after_offline and not getattr(self.bot, "delivery_store", None):
            state.messages = await self._delete_messages(state)
        if state.last_info and not self.config.ping_once_per_stream and not platform.config.delete_after_offline and getattr(self.bot, "delivery_store", None):
            self.bot.delivery_store.delete(self.bot.delivery_store.key(platform, state.last_info))
        # Reset state so the next stream notifies again.
        state.is_live = False
        state.current_key = None
        if not platform.config.delete_after_offline:
            state.messages = []
        state.last_info = None

    # -- delivery ----------------------------------------------------------
    async def _find_pending(self, record: dict, marker: str) -> int | None:
        """Search the original channel before retrying an uncertain send."""
        channel = self.bot.get_channel(int(record["channel_id"]))
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise RuntimeError("Original notification channel is unavailable")
        after = datetime.fromtimestamp(record["started"], timezone.utc)
        async for message in channel.history(limit=None, after=after):
            if str(message.author.id) != record["author_id"] or message.webhook_id:
                continue
            content = getattr(message, "content", "") or ""
            if text_marker(marker) in content or any(marker in (embed.footer.text or "") for embed in message.embeds):
                return message.id
        return None

    async def _recover_expired(self, platform: BasePlatform, state: StreamState, live_key: str | None) -> None:
        """Finish pending deletions after an offline observation or restart."""
        store = self.bot.delivery_store
        state.delete_error = None
        for key, stream_key, record in store.outstanding(platform):
            if stream_key == live_key:
                continue
            if not platform.config.delete_after_offline:
                store.mark_deleted(key)
                continue
            try:
                message_id = int(record["message_id"]) if record["message_id"] else await self._find_pending(record, record["marker"])
                if message_id is not None:
                    channel = self.bot.get_channel(int(record["channel_id"]))
                    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                        raise RuntimeError("Original notification channel unavailable")
                    try:
                        message = await channel.fetch_message(message_id)
                        await message.delete()
                    except discord.NotFound:
                        pass
                if self.config.ping_once_per_stream:
                    store.mark_deleted(key)
                else:
                    store.delete(key)
                state.messages = [ref for ref in state.messages if ref.message_id != message_id]
            except (discord.DiscordException, aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                state.delete_error = error_code(exc, "delete_failed")
                log.warning("Could not recover notification deletion: %s", type(exc).__name__)

    async def _send_notification(self, state: StreamState, info: StreamInfo, config: Config, platform: BasePlatform) -> bool:
        """Deliver once per stream and recover uncertain sends from channel history."""
        server = config.servers[0]
        store = getattr(self.bot, "delivery_store", None)
        key = store.key(platform, info) if store else None
        record = store.get(key) if store else None
        if record and record["is_webhook"]:
            state.delivery_error = "old_delivery"
            return False
        if record and record["message_id"]:
            state.messages = [] if record["deleted"] else [SentMessage(server.guild_id, int(record["channel_id"]), int(record["message_id"]))]
            state.delivery_error = None
            return True

        embed = build_live_embed(info, config)
        try:
            channel = self.bot.get_channel(server.notification_channel_id)
            author_id = self.bot.user.id if store else None
            if not isinstance(channel, (discord.TextChannel, discord.Thread)) or channel.guild.id != server.guild_id:
                state.delivery_error = "channel_unavailable"
                return False
            if record:
                try:
                    found = await self._find_pending(record, record["marker"])
                except (discord.DiscordException, aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
                    state.delivery_error = "history_unavailable"
                    return False
                if found is not None:
                    store.sent(key, record["channel_id"], found)
                    state.messages = [SentMessage(server.guild_id, int(record["channel_id"]), found)]
                    state.delivery_error = None
                    return True
                if (record["channel_id"], record["author_id"]) != (str(channel.id), str(author_id)):
                    store.retarget(key, channel.id, author_id)
            elif store:
                record = store.begin(key, channel.id, author_id)
            if record and config.message_type == "embed":
                embed.set_footer(text=f"{info.platform.capitalize()} • Stream Notifier • {record['marker']}")
            role = f"<@&{server.live_role_id}>" if server.live_role_id else None
            content = build_message_content(info, config, role)
            if config.message_type == "plain":
                if info.url not in content:
                    content += "\n" + info.url
                if record:
                    content += text_marker(record['marker'])
            allowed = discord.AllowedMentions(everyone=False,
                users=[discord.Object(id=int(config.discord_user_id))] if config.discord_user_id else [],
                roles=[discord.Object(id=server.live_role_id)] if server.live_role_id else [], replied_user=False)
            message = await channel.send(content=content, embed=embed if config.message_type == "embed" else None,
                                         view=StreamButtons(info, config) if config.message_type == "embed" else None,
                                         allowed_mentions=allowed)
            if store:
                store.sent(key, channel.id, message.id)
            state.messages.append(SentMessage(server.guild_id, channel.id, message.id))
            self.stats.notifications_sent += 1
            self.stats.per_platform[info.platform] = self.stats.per_platform.get(info.platform, 0) + 1
            state.delivery_error = None
            return True
        except (discord.DiscordException, aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            state.delivery_error = error_code(exc, "send_failed")
            log.error("Notification not confirmed; retry deferred: %s", type(exc).__name__)
            return False

    async def _refresh_messages(self, state: StreamState, info: StreamInfo) -> None:
        """Edit existing notifications with fresh thumbnail / viewer info."""
        if not state.messages:
            return
        embed = build_live_embed(info, self.config)
        for ref in state.messages:
            channel = self.bot.get_channel(ref.channel_id)
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue
            try:
                message = await channel.fetch_message(ref.message_id)
                await message.edit(embed=embed)
            except discord.DiscordException:
                # Message may have been deleted manually; ignore.
                continue

    async def _delete_messages(self, state: StreamState) -> list[SentMessage]:
        remaining = []
        for ref in state.messages:
            try:
                channel = self.bot.get_channel(ref.channel_id)
                if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                    remaining.append(ref)
                    continue
                message = await channel.fetch_message(ref.message_id)
                await message.delete()
            except discord.NotFound:
                continue
            except (discord.DiscordException, aiohttp.ClientError, asyncio.TimeoutError):
                remaining.append(ref)
        state.delete_error = "delete_failed" if remaining else None
        return remaining

    # -- presence ----------------------------------------------------------
    async def _update_presence(self, live_now: list[StreamInfo]) -> None:
        """Reflect live status in the bot presence."""
        activity: discord.BaseActivity
        if live_now:
            info = live_now[0]
            activity = discord.Streaming(
                name=f"{info.channel_name} LIVE",
                url=info.url if info.url.startswith("http") else "https://twitch.tv/discord",
            )
        else:
            activity = discord.Activity(
                type=discord.ActivityType.watching,
                name=self.config.presence_idle,
            )
        await self.bot.change_presence(status=discord.Status.online, activity=activity)


async def setup(bot: commands.Bot) -> None:  # pragma: no cover - discord entrypoint
    """discord.py extension entrypoint (unused; cog added explicitly)."""
    raise RuntimeError("StreamMonitor is added manually in main.py")
