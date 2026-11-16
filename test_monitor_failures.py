import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from monitor import StreamMonitor
from platforms.base import PlatformError, StreamInfo
from test_config_servers import ConfigServerTests


class MonitorFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        config = ConfigServerTests().load({'servers': [{'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'A', 'youtube_channel_id': 'UC1'}]}]})
        self.bot = type('Bot', (), {'change_presence': AsyncMock()})()
        self.monitor = StreamMonitor(self.bot, config)
        await self.monitor.cog_load()
        self.platform = self.monitor.platforms[0]
        self.info = StreamInfo(platform='youtube', title='Live', url='https://example.com', channel_name='A', stream_id='123')

    async def asyncTearDown(self):
        await self.monitor.cog_unload()

    async def test_delivery_failure_is_retried_without_duplicate_after_success(self):
        self.platform.get_stream_info = AsyncMock(return_value=self.info)
        self.monitor._send_notification = AsyncMock(side_effect=[False, True])
        self.monitor._refresh_messages = AsyncMock()
        await self.monitor.check_all()
        self.assertIsNone(self.monitor.state[self.platform].notified_key)
        await self.monitor.check_all()
        await self.monitor.check_all()
        self.assertEqual(self.monitor._send_notification.await_count, 2)
        self.assertEqual(self.monitor.state[self.platform].notified_key, self.info.unique_key)
        self.monitor._refresh_messages.assert_awaited_once()

    async def test_api_failure_keeps_live_state_and_does_not_send_again(self):
        self.platform.get_stream_info = AsyncMock(side_effect=[self.info, PlatformError('unavailable'), self.info])
        self.monitor._send_notification = AsyncMock(return_value=True)
        self.monitor._refresh_messages = AsyncMock()
        for _ in range(3):
            await self.monitor.check_all()
        self.assertTrue(self.monitor.state[self.platform].is_live)
        self.monitor._send_notification.assert_awaited_once()
        self.assertEqual(self.bot.change_presence.await_count, 3)
        self.assertEqual(self.bot.change_presence.await_args_list[1].kwargs['activity'].name, 'A LIVE')

    async def test_retry_failure_does_not_include_secret(self):
        call = AsyncMock(side_effect=aiohttp.ClientError('secret-in-url'))
        with patch('platforms.base.asyncio.sleep', new=AsyncMock()):
            with self.assertRaises(PlatformError) as error:
                await self.platform._with_retry(call, retries=3, what='Twitch token')
        self.assertNotIn('secret-in-url', str(error.exception))
        self.assertEqual(call.await_count, 3)

    async def test_discord_send_failure_then_success_updates_statistics(self):
        from types import SimpleNamespace
        import monitor as monitor_module

        class Channel:
            id = 2
            guild = SimpleNamespace(id=1)
            send = AsyncMock(side_effect=[aiohttp.ClientError('network'), SimpleNamespace(id=99)])
            fetch_message = AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))

        channel = Channel()
        self.bot.get_channel = lambda channel_id: channel
        self.platform.get_stream_info = AsyncMock(return_value=self.info)
        with patch.object(monitor_module.discord, 'TextChannel', Channel):
            await self.monitor.check_all()
            self.assertEqual(self.monitor.stats.notifications_sent, 0)
            await self.monitor.check_all()
            await self.monitor.check_all()
        self.assertEqual(channel.send.await_count, 2)
        self.assertEqual(self.monitor.stats.notifications_sent, 1)
        self.assertEqual(self.monitor.state[self.platform].messages[0].message_id, 99)
