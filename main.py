"""Stream Notifier Discord bot - application entrypoint.

Loads & validates configuration, connects to Discord, registers slash commands
and starts monitoring every configured platform automatically.

Run with:  python main.py
"""

from __future__ import annotations

import asyncio

import discord
from discord.ext import commands

from app_paths import CONFIG, DELIVERIES
from commands.admin import setup_admin_commands
from delivery_store import DeliveryStore
from monitor import StreamMonitor
from utils.config import Config, ConfigError, load_config
from utils.logger import get_logger, setup_logging

log = setup_logging()


class StreamNotifierBot(commands.Bot):
    """The Discord bot wired up with the monitor and admin commands."""

    def __init__(self, config: Config, previous_monitor: StreamMonitor | None = None, rejected_token: str | None = None, delivery_store: DeliveryStore | None = None) -> None:
        # Only the intents we actually need - keeps the bot privacy-friendly.
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.config = config
        self.monitor: StreamMonitor | None = None
        self.delivery_store = delivery_store
        self.previous_monitor = previous_monitor
        self.rejected_token = rejected_token
        self.next_config: Config | None = None

    async def setup_hook(self) -> None:
        """Register cogs and sync slash commands before connecting."""
        self.monitor = StreamMonitor(self, self.config)
        await self.add_cog(self.monitor)
        if self.previous_monitor:
            previous = self.previous_monitor
            old_state = {previous.platform_key(p): previous.state[p] for p in previous.platforms}
            for platform in self.monitor.platforms:
                key = self.monitor.platform_key(platform)
                if key in old_state:
                    self.monitor.state[platform] = old_state[key]
            self.monitor.stats = previous.stats
        self.monitor.rejected_token = self.rejected_token
        await setup_admin_commands(self, self.config)

        # Sync commands to each configured guild for instant availability.
        for server in self.config.servers:
            guild = discord.Object(id=server.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced slash commands to guild %s", server.guild_id)

        # The loop waits internally until the bot is ready.
        self.monitor.start()

    async def on_ready(self) -> None:
        """Log a friendly banner once connected."""
        assert self.user is not None
        log.info("Connected as %s (id: %s)", self.user, self.user.id)
        log.info(
            "Watching for live streams every %ss across %d platform(s).",
            self.config.poll_interval,
            len(self.monitor.platforms) if self.monitor else 0,
        )
        # Set the idle presence immediately on connect.
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=self.config.presence_idle,
            ),
        )


async def run() -> None:
    """Load configuration and run the bot until interrupted."""
    try:
        config = load_config(CONFIG, allow_missing=True)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        raise SystemExit(1) from exc

    log.info("Configuration loaded and validated successfully.")
    store = DeliveryStore(DELIVERIES)
    try:
        await run_with_store(config, store)
    finally:
        store.close()


async def run_with_store(config: Config, store: DeliveryStore) -> None:
    previous_monitor = None
    fallback = None
    rejected_token = None
    while True:
        bot = StreamNotifierBot(config, previous_monitor, rejected_token, store)
        try:
            async with bot:
                await bot.start(config.discord_token)
        except discord.LoginFailure:
            if fallback is None:
                log.error("Invalid Discord token. Check DISCORD_TOKEN in your .env file.")
                raise SystemExit(1)
            log.error("New Discord token was rejected during reconnect; restoring the previous token.")
            rejected_token = config.discord_token
            config = fallback
            fallback = None
            continue
        if bot.next_config is None:
            break
        previous_monitor = bot.monitor
        fallback = bot.config
        config = bot.next_config
        rejected_token = None
        log.info("Reconnecting Discord with the updated bot token.")



def main() -> None:
    """Synchronous wrapper used as the console entrypoint."""
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        get_logger().info("Shutting down (keyboard interrupt).")


if __name__ == "__main__":
    main()
