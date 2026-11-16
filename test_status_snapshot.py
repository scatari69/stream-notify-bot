import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from monitor import StreamMonitor
from platforms.base import PlatformError, StreamInfo
from test_config_servers import ConfigServerTests


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_reports_live_then_unknown_on_api_error(self):
        config = ConfigServerTests().load({'servers': [{'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'Alice', 'youtube_channel_id': 'UC1'}]}]})
        bot = type('Bot', (), {'change_presence': AsyncMock()})()
        monitor = StreamMonitor(bot, config)
        await monitor.cog_load()
        info = StreamInfo(platform='youtube', title='Live', url='https://example.com', channel_name='Alice', stream_id='123')
        monitor.platforms[0].get_stream_info = AsyncMock(side_effect=[info, PlatformError('temporary')])
        monitor._send_notification = AsyncMock(return_value=True)
        try:
            with tempfile.TemporaryDirectory() as folder, patch('monitor.STATUS', Path(folder) / 'status.json'):
                await monitor.check_all()
                first = json.loads((Path(folder) / 'status.json').read_text())
                self.assertEqual(first['streams'][0]['status'], 'online')
                self.assertEqual(first['streams'][0]['guild_id'], 1)
                await monitor.check_all()
                second = json.loads((Path(folder) / 'status.json').read_text())
                self.assertEqual(second['streams'][0]['status'], 'unknown')
                self.assertTrue(second['streams'][0]['checked_at'])
        finally:
            await monitor.cog_unload()
