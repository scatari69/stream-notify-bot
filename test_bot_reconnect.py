import unittest
from dataclasses import replace
from unittest.mock import patch

import discord
import main
from test_config_servers import ConfigServerTests


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_new_token_restores_active_config_and_state(self):
        original = ConfigServerTests().load({'servers': [{'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'A', 'youtube_channel_id': 'UC1'}]}]})
        active = replace(original, poll_interval=90)
        updated = replace(active, discord_token='replacement')
        instances = []

        class FakeBot:
            def __init__(self, config, previous, rejected, store):
                self.config, self.previous, self.rejected = config, previous, rejected
                self.monitor = object()
                self.next_config = None
                instances.append(self)
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return None
            async def start(self, token):
                if len(instances) == 1:
                    self.config = active
                    self.next_config = updated
                elif len(instances) == 2:
                    raise discord.LoginFailure('invalid')

        with patch('main.load_config', return_value=original), patch('main.StreamNotifierBot', FakeBot), patch('main.DeliveryStore'):
            await main.run()
        self.assertEqual([b.config for b in instances], [active, updated, active])
        self.assertIs(instances[2].previous, instances[0].monitor)
        self.assertEqual(instances[2].rejected, 'replacement')

    async def test_new_connection_restores_stream_state(self):
        from unittest.mock import AsyncMock
        from monitor import StreamMonitor
        config = ConfigServerTests().load({'servers': [{'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'A', 'youtube_channel_id': 'UC1'}]}]})
        previous = StreamMonitor(type('Bot', (), {})(), config)
        await previous.cog_load()
        previous.state[previous.platforms[0]].notified_key = 'youtube:live'
        bot = main.StreamNotifierBot(config, previous)
        async def add_monitor(cog):
            await cog.cog_load()
        try:
            with patch.object(bot, 'add_cog', side_effect=add_monitor), patch('main.setup_admin_commands', new=AsyncMock()), patch.object(bot.tree, 'copy_global_to'), patch.object(bot.tree, 'sync', new=AsyncMock()), patch.object(StreamMonitor, 'start'):
                await bot.setup_hook()
            self.assertEqual(bot.monitor.state[bot.monitor.platforms[0]].notified_key, 'youtube:live')
            self.assertIs(bot.monitor.stats, previous.stats)
        finally:
            await previous.cog_unload()
            if bot.monitor:
                await bot.monitor.cog_unload()
