import unittest
from unittest.mock import AsyncMock, patch

from monitor import StreamMonitor
from platforms.base import StreamInfo
from test_config_servers import ConfigServerTests


class MonitorServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_route_only_to_their_own_server(self):
        helper = ConfigServerTests()
        config = helper.load({'servers': [
            {'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'A', 'youtube_channel_id': 'UC1'}]},
            {'guild_id': 3, 'notification_channel_id': 4, 'streams': [{'name': 'B', 'youtube_channel_id': 'UC2'}]},
        ]})
        bot = type('Bot', (), {'change_presence': AsyncMock()})()
        monitor = StreamMonitor(bot, config)
        await monitor.cog_load()
        try:
            for platform in monitor.platforms:
                platform.get_stream_info = AsyncMock(return_value=StreamInfo(
                    platform='youtube', title='Live', url='https://example.com',
                    channel_name=platform.config.streamer_name, stream_id='same'))
            monitor._send_notification = AsyncMock()
            await monitor.check_all()
            await monitor.check_all()
            self.assertEqual(monitor._send_notification.await_count, 2)
            calls = monitor._send_notification.await_args_list
            self.assertEqual([call.args[2].servers[0].guild_id for call in calls], [1, 3])
            self.assertIsNot(calls[0].args[0], calls[1].args[0])
        finally:
            await monitor.cog_unload()

    async def test_reload_preserves_unchanged_stream_state(self):
        helper = ConfigServerTests()
        old = helper.load({'servers': [{'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'A', 'youtube_channel_id': 'UC1'}]}]})
        new = helper.load({'servers': [{'guild_id': 1, 'notification_channel_id': 7, 'streams': [{'name': 'A renamed', 'youtube_channel_id': 'UC1'}, {'name': 'B', 'youtube_channel_id': 'UC2'}]}]})
        bot = type('Bot', (), {})()
        monitor = StreamMonitor(bot, old)
        await monitor.cog_load()
        try:
            first = monitor.platforms[0]
            monitor.state[first].notified_key = 'youtube:123'
            await monitor.apply_config(new)
            self.assertEqual(monitor.state[monitor.platforms[0]].notified_key, 'youtube:123')
            self.assertEqual(monitor.platforms[0].config.servers[0].notification_channel_id, 7)
            self.assertIsNone(monitor.state[monitor.platforms[1]].notified_key)
        finally:
            await monitor.cog_unload()

    async def test_reload_keeps_last_valid_configuration(self):
        from utils.config import ConfigError
        helper = ConfigServerTests()
        config = helper.load({'servers': [{'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'A', 'youtube_channel_id': 'UC1'}]}]})
        bot = type('Bot', (), {})()
        monitor = StreamMonitor(bot, config)
        await monitor.cog_load()
        try:
            with patch('monitor.load_config', side_effect=ConfigError('invalid')):
                await monitor.reload_loop.coro(monitor)
            self.assertEqual(monitor.config, config)
            self.assertEqual(len(monitor.platforms), 1)
        finally:
            await monitor.cog_unload()

    async def test_token_rotation_only_accepts_same_bot(self):
        import asyncio
        from dataclasses import replace
        from types import SimpleNamespace

        config = ConfigServerTests().load({'servers': [{'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'A', 'youtube_channel_id': 'UC1'}]}]})
        bot = SimpleNamespace(user=SimpleNamespace(id=42), close=AsyncMock(), next_config=None)
        monitor = StreamMonitor(bot, config)
        await monitor.cog_load()

        class Response:
            status = 200
            def __init__(self, user_id):
                self.user_id = user_id
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                pass
            async def json(self):
                return {'id': self.user_id}

        try:
            new = replace(config, discord_token='new-token')
            with patch('monitor.load_config', return_value=new), patch.object(monitor.session, 'get', return_value=Response('99')):
                await monitor.reload_loop.coro(monitor)
            self.assertIsNone(bot.next_config)
            bot.close.assert_not_awaited()
            monitor.rejected_token = None
            with patch('monitor.load_config', return_value=new), patch.object(monitor.session, 'get', return_value=Response('42')):
                await monitor.reload_loop.coro(monitor)
                await asyncio.sleep(0)
            self.assertEqual(bot.next_config, new)
            bot.close.assert_awaited_once()
        finally:
            await monitor.cog_unload()

    async def test_paused_stream_is_checked_without_notification_then_resumes(self):
        from dataclasses import replace
        helper = ConfigServerTests()
        config = helper.load({'servers': [{'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'A', 'youtube_channel_id': 'UC1', 'paused': True}]}]})
        bot = type('Bot', (), {'change_presence': AsyncMock()})()
        monitor = StreamMonitor(bot, config)
        await monitor.cog_load()
        info = StreamInfo(platform='youtube', title='Live', url='https://example.com', channel_name='A', stream_id='123')
        try:
            monitor.platforms[0].get_stream_info = AsyncMock(return_value=info)
            monitor._send_notification = AsyncMock(return_value=True)
            await monitor.check_all()
            state = monitor.state[monitor.platforms[0]]
            self.assertTrue(state.is_live)
            self.assertIsNone(state.notified_key)
            monitor._send_notification.assert_not_awaited()
            stream = {**config.servers[0].streams[0], 'paused': False}
            resumed = replace(config, servers=[replace(config.servers[0], streams=[stream])])
            await monitor.apply_config(resumed)
            monitor.platforms[0].get_stream_info = AsyncMock(return_value=info)
            await monitor.check_all()
            monitor._send_notification.assert_awaited_once()
            self.assertEqual(monitor.state[monitor.platforms[0]].notified_key, info.unique_key)
        finally:
            await monitor.cog_unload()

    async def test_paused_setting_applies_to_each_platform_and_server_independently(self):
        from dataclasses import replace
        helper = ConfigServerTests()
        config = helper.load({'servers': [
            {'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'A', 'youtube_channel_id': 'UC1', 'paused': True}]},
            {'guild_id': 3, 'notification_channel_id': 4, 'streams': [{'name': 'B', 'youtube_channel_id': 'UC2'}]},
        ]})
        stream = {**config.servers[0].streams[0], 'twitch_username': 'alice'}
        config = replace(config, twitch_client_id='id', twitch_client_secret='secret', servers=[replace(config.servers[0], streams=[stream]), config.servers[1]])
        bot = type('Bot', (), {'change_presence': AsyncMock()})()
        monitor = StreamMonitor(bot, config)
        await monitor.cog_load()
        try:
            for platform in monitor.platforms:
                platform.get_stream_info = AsyncMock(return_value=StreamInfo(platform=platform.name, title='Live', url='https://example.com', channel_name='A', stream_id='123'))
            monitor._send_notification = AsyncMock(return_value=True)
            await monitor.check_all()
            self.assertEqual(len(monitor.platforms), 3)
            monitor._send_notification.assert_awaited_once()
            self.assertEqual(monitor._send_notification.await_args.args[2].servers[0].guild_id, 3)
            self.assertTrue(all(monitor.state[p].is_live for p in monitor.platforms))
        finally:
            await monitor.cog_unload()
