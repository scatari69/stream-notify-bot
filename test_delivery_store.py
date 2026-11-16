import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import monitor as monitor_module
from delivery_store import DeliveryStore
from monitor import StreamMonitor
from platforms.base import StreamInfo
from test_config_servers import ConfigServerTests


class Channel:
    id = 2
    guild = SimpleNamespace(id=1)

    def __init__(self):
        self.messages = []
        self.lose_response = False
        self.history_error = False
        self.send_count = 0
        self.deleted = []

    async def send(self, **kwargs):
        self.send_count += 1
        message = SimpleNamespace(id=100 + self.send_count, author=SimpleNamespace(id=55), webhook_id=None, content=kwargs['content'], embeds=[kwargs['embed']] if kwargs['embed'] else [])
        self.messages.append(message)
        if self.lose_response:
            raise aiohttp.ClientError('response lost')
        return message

    async def history(self, **kwargs):
        if self.history_error:
            raise aiohttp.ClientError('history unavailable')
        for message in self.messages:
            yield message

    async def fetch_message(self, message_id):
        async def delete():
            self.deleted.append(message_id)
        return SimpleNamespace(edit=AsyncMock(), delete=delete)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'data' / 'state.sqlite3'
        self.store = DeliveryStore(self.path)
        self.channel = Channel()
        self.bot = SimpleNamespace(user=SimpleNamespace(id=55), delivery_store=self.store,
                                   get_channel=lambda _: self.channel, change_presence=AsyncMock())
        self.config = ConfigServerTests().load({'servers': [{'guild_id': 1, 'notification_channel_id': 2,
                          'streams': [{'name': 'A', 'youtube_channel_id': 'UC1'}]}]})
        self.info = StreamInfo(platform='youtube', title='Live', url='https://example.com', channel_name='A', stream_id='123')
        self.monitor = await self.new_monitor()
        self.channel_patch = patch.object(monitor_module.discord, 'TextChannel', Channel)
        self.channel_patch.start()

    async def new_monitor(self):
        monitor = StreamMonitor(self.bot, self.config)
        await monitor.cog_load()
        monitor.platforms[0].get_stream_info = AsyncMock(return_value=self.info)
        return monitor

    async def asyncTearDown(self):
        await self.monitor.cog_unload()
        self.channel_patch.stop()
        self.store.close()
        self.folder.cleanup()

    async def restart(self):
        await self.monitor.cog_unload()
        self.store.close()
        self.store = DeliveryStore(self.path)
        self.bot.delivery_store = self.store
        self.monitor = await self.new_monitor()

    async def test_sent_message_survives_restart(self):
        await self.monitor.check_all()
        await self.restart()
        await self.monitor.check_all()
        self.assertEqual(self.channel.send_count, 1)
        self.assertEqual(self.monitor.state[self.monitor.platforms[0]].messages[0].message_id, 101)

    async def test_lost_response_is_found_after_restart(self):
        self.channel.lose_response = True
        await self.monitor.check_all()
        await self.restart()
        self.channel.lose_response = False
        await self.monitor.check_all()
        self.assertEqual(self.channel.send_count, 1)
        record = self.store.get(self.store.key(self.monitor.platforms[0], self.info))
        self.assertEqual(record['message_id'], '101')

    async def test_unavailable_history_prevents_duplicate_retry(self):
        self.channel.lose_response = True
        await self.monitor.check_all()
        self.channel.lose_response = False
        self.channel.history_error = True
        await self.monitor.check_all()
        self.assertEqual(self.channel.send_count, 1)
        self.assertIsNone(self.monitor.state[self.monitor.platforms[0]].notified_key)
        self.channel.history_error = False
        await self.monitor.check_all()
        self.assertEqual(self.channel.send_count, 1)

    async def test_no_message_found_retries_after_restart(self):
        key = self.store.key(self.monitor.platforms[0], self.info)
        self.store.begin(key, self.channel.id, 55)
        await self.restart()
        await self.monitor.check_all()
        self.assertEqual(self.channel.send_count, 1)
        self.assertIsNotNone(self.store.get(key)['message_id'])

    async def test_storage_failure_prevents_sending(self):
        import sqlite3
        with patch.object(self.store, 'begin', side_effect=sqlite3.OperationalError('disk full')):
            await self.monitor.check_all()
        self.assertEqual(self.channel.send_count, 0)

    async def test_pending_follows_channel_change_before_retry(self):
        from dataclasses import replace
        key = self.store.key(self.monitor.platforms[0], self.info)
        self.store.begin(key, 2, 55)
        second = Channel()
        second.id = 9
        second.lose_response = True
        self.bot.get_channel = lambda channel_id: {2: self.channel, 9: second}.get(channel_id)
        server = replace(self.config.servers[0], notification_channel_id=9)
        self.config = replace(self.config, servers=[server])
        await self.monitor.apply_config(self.config)
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=self.info)
        await self.monitor.check_all()
        self.assertEqual(self.store.get(key)['channel_id'], '9')
        await self.restart()
        second.lose_response = False
        await self.monitor.check_all()
        self.assertEqual(second.send_count, 1)
        self.assertEqual(self.store.get(key)['message_id'], '101')

    async def test_plain_message_and_retry_have_no_embed_or_duplicate(self):
        from dataclasses import replace
        server = replace(self.config.servers[0], message_type='plain', custom_message='Сейчас {name}')
        self.config = replace(self.config, servers=[server])
        await self.monitor.apply_config(self.config)
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=self.info)
        self.channel.lose_response = True
        await self.monitor.check_all()
        sent = self.channel.messages[0]
        self.assertEqual(sent.embeds, [])
        self.assertIn('Сейчас A', sent.content)
        self.assertIn(self.info.url, sent.content)
        await self.restart()
        self.channel.lose_response = False
        await self.monitor.check_all()
        self.assertEqual(self.channel.send_count, 1)

    async def test_delete_setting_applies_per_server(self):
        from dataclasses import replace
        server = replace(self.config.servers[0], delete_after_offline=True)
        self.config = replace(self.config, servers=[server])
        await self.monitor.apply_config(self.config)
        self.monitor.platforms[0].get_stream_info = AsyncMock(side_effect=[self.info, None])
        await self.monitor.check_all()
        await self.monitor.check_all()
        self.assertEqual(self.channel.deleted, [101])

    async def test_disabled_delete_leaves_notification(self):
        self.monitor.platforms[0].get_stream_info = AsyncMock(side_effect=[self.info, None])
        await self.monitor.check_all()
        await self.monitor.check_all()
        self.assertEqual(self.channel.deleted, [])

    async def test_deletion_failure_is_retried_when_still_offline(self):
        from dataclasses import replace
        server = replace(self.config.servers[0], delete_after_offline=True)
        self.config = replace(self.config, servers=[server])
        await self.monitor.apply_config(self.config)
        self.monitor.platforms[0].get_stream_info = AsyncMock(side_effect=[self.info, None, None])
        await self.monitor.check_all()
        delete = AsyncMock(side_effect=[aiohttp.ClientError('down'), None])
        self.channel.fetch_message = AsyncMock(return_value=SimpleNamespace(delete=delete))
        await self.monitor.check_all()
        self.assertEqual(len(self.monitor.state[self.monitor.platforms[0]].messages), 1)
        await self.monitor.check_all()
        self.assertEqual(delete.await_count, 2)
        self.assertEqual(self.monitor.state[self.monitor.platforms[0]].messages, [])

    async def test_embed_contains_preview_and_custom_message(self):
        from dataclasses import replace
        self.info.thumbnail = 'https://example.com/preview.png'
        server = replace(self.config.servers[0], message_type='embed', custom_message='Привет {name}!')
        self.config = replace(self.config, servers=[server])
        await self.monitor.apply_config(self.config)
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=self.info)
        await self.monitor.check_all()
        message = self.channel.messages[0]
        self.assertEqual(message.content, 'Привет A!')
        self.assertEqual(message.embeds[0].image.url, self.info.thumbnail)

    async def test_streamer_placeholder_mentions_only_linked_user(self):
        from dataclasses import replace
        from utils.embeds import build_message_content
        server = replace(self.config.servers[0], custom_message='Сейчас {streamer}: {url}')
        server.streams[0]['discord_user_id'] = '1234'
        self.config = replace(self.config, servers=[server])
        await self.monitor.apply_config(self.config)
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=self.info)
        captured = {}
        original_send = self.channel.send
        async def save_args(**kwargs):
            captured.update(kwargs)
            return await original_send(**kwargs)
        self.channel.send = save_args
        await self.monitor.check_all()
        self.assertIn('<@1234>', captured['content'])
        mentions = captured['allowed_mentions']
        self.assertEqual([user.id for user in mentions.users], [1234])
        self.assertFalse(mentions.everyone)
        config = replace(self.monitor.platforms[0].config, discord_user_id='')
        self.assertIn('Сейчас A', build_message_content(self.info, config, None))
        self.assertNotIn('<@', build_message_content(self.info, config, None))


    async def test_stream_ended_during_downtime_is_deleted_after_restart(self):
        from dataclasses import replace
        self.config = replace(self.config, servers=[replace(self.config.servers[0], delete_after_offline=True)])
        await self.monitor.apply_config(self.config)
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=self.info)
        await self.monitor.check_all()
        await self.restart()
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=None)
        await self.monitor.check_all()
        await self.monitor.check_all()
        self.assertEqual(self.channel.deleted, [101])
        self.assertEqual(self.store.outstanding(self.monitor.platforms[0]), [])

    async def test_uncertain_platform_status_does_not_delete_after_restart(self):
        from dataclasses import replace
        from platforms.base import PlatformError
        self.config = replace(self.config, servers=[replace(self.config.servers[0], delete_after_offline=True)])
        await self.monitor.apply_config(self.config)
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=self.info)
        await self.monitor.check_all()
        await self.restart()
        self.monitor.platforms[0].get_stream_info = AsyncMock(side_effect=PlatformError('offline unknown'))
        await self.monitor.check_all()
        self.assertEqual(self.channel.deleted, [])
        self.assertEqual(len(self.store.outstanding(self.monitor.platforms[0])), 1)

    async def test_lost_send_is_found_and_deleted_after_downtime(self):
        from dataclasses import replace
        self.config = replace(self.config, servers=[replace(self.config.servers[0], delete_after_offline=True)])
        await self.monitor.apply_config(self.config)
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=self.info)
        self.channel.lose_response = True
        await self.monitor.check_all()
        await self.restart()
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=None)
        await self.monitor.check_all()
        self.assertEqual(self.channel.send_count, 1)
        self.assertEqual(self.channel.deleted, [101])

    async def test_failed_deletion_is_retried_after_another_restart(self):
        from dataclasses import replace
        self.config = replace(self.config, servers=[replace(self.config.servers[0], delete_after_offline=True)])
        await self.monitor.apply_config(self.config)
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=self.info)
        await self.monitor.check_all()
        await self.restart()
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=None)
        self.channel.fetch_message = AsyncMock(side_effect=aiohttp.ClientError('unavailable'))
        await self.monitor.check_all()
        self.assertEqual(self.monitor.state[self.monitor.platforms[0]].delete_error, 'delete_failed')
        await self.restart()
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=None)
        self.channel.fetch_message = Channel.fetch_message.__get__(self.channel)
        await self.monitor.check_all()
        self.assertEqual(self.channel.deleted, [101])
        self.assertIsNone(self.monitor.state[self.monitor.platforms[0]].delete_error)

    async def test_old_messages_are_not_deleted_after_enabling_setting_later(self):
        from dataclasses import replace
        self.monitor.platforms[0].get_stream_info = AsyncMock(side_effect=[self.info, None])
        await self.monitor.check_all()
        await self.monitor.check_all()
        self.config = replace(self.config, servers=[replace(self.config.servers[0], delete_after_offline=True)])
        await self.monitor.apply_config(self.config)
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=None)
        await self.monitor.check_all()
        self.assertEqual(self.channel.deleted, [])

    async def test_new_stream_after_downtime_deletes_old_and_announces_new(self):
        from dataclasses import replace
        self.config = replace(self.config, servers=[replace(self.config.servers[0], delete_after_offline=True)])
        await self.monitor.apply_config(self.config)
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=self.info)
        await self.monitor.check_all()
        await self.restart()
        new_info = StreamInfo(platform='youtube', title='New', url='https://example.com/new', channel_name='A', stream_id='456')
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=new_info)
        await self.monitor.check_all()
        self.assertEqual(self.channel.deleted, [101])
        self.assertEqual(self.channel.send_count, 2)
        self.assertEqual(self.monitor.state[self.monitor.platforms[0]].notified_key, new_info.unique_key)

    async def test_deletion_is_isolated_between_servers(self):
        from dataclasses import replace
        first = replace(self.config.servers[0], delete_after_offline=True)
        second = replace(first, guild_id=3, notification_channel_id=9)
        self.config = replace(self.config, servers=[first, second])
        other = Channel()
        other.id = 9
        other.guild = SimpleNamespace(id=3)
        self.bot.get_channel = lambda channel_id: {2: self.channel, 9: other}.get(channel_id)
        await self.monitor.apply_config(self.config)
        for platform in self.monitor.platforms:
            platform.get_stream_info = AsyncMock(return_value=self.info)
        await self.monitor.check_all()
        await self.restart()
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=None)
        self.monitor.platforms[1].get_stream_info = AsyncMock(return_value=self.info)
        await self.monitor.check_all()
        self.assertEqual(self.channel.deleted, [101])
        self.assertEqual(other.deleted, [])
        self.assertEqual(other.send_count, 1)

    async def test_delivery_error_is_published_and_cleared_after_recovery(self):
        import json
        status_path = Path(self.folder.name) / 'status.json'
        self.channel.lose_response = True
        with patch('monitor.STATUS', status_path):
            await self.monitor.check_all()
            self.assertEqual(json.loads(status_path.read_text())['streams'][0]['delivery_error'], 'send_failed')
            self.channel.lose_response = False
            self.channel.history_error = True
            await self.monitor.check_all()
            self.assertEqual(json.loads(status_path.read_text())['streams'][0]['delivery_error'], 'history_unavailable')
            self.channel.history_error = False
            await self.monitor.check_all()
            self.assertIsNone(json.loads(status_path.read_text())['streams'][0]['delivery_error'])

    async def test_deleted_message_is_not_restored_or_resent_for_same_stream(self):
        from dataclasses import replace
        self.config = replace(self.config, servers=[replace(self.config.servers[0], delete_after_offline=True)])
        await self.monitor.apply_config(self.config)
        self.monitor.platforms[0].get_stream_info = AsyncMock(side_effect=[self.info, None, self.info])
        await self.monitor.check_all()
        await self.monitor.check_all()
        await self.monitor.check_all()
        self.assertEqual(self.channel.send_count, 1)
        self.assertEqual(self.channel.deleted, [101])
        self.assertEqual(self.monitor.state[self.monitor.platforms[0]].messages, [])

    async def test_existing_delivery_database_is_preserved_when_upgraded(self):
        import sqlite3
        old_path = Path(self.folder.name) / 'old.sqlite3'
        db = sqlite3.connect(old_path)
        db.execute('CREATE TABLE deliveries (stream_key TEXT PRIMARY KEY, marker TEXT NOT NULL, channel_id TEXT NOT NULL, author_id TEXT NOT NULL, is_webhook INTEGER NOT NULL, started REAL NOT NULL, message_id TEXT)')
        db.execute('INSERT INTO deliveries VALUES (?, ?, ?, ?, ?, ?, ?)', ('key', 'marker', '2', '55', 0, 1.0, '101'))
        db.commit()
        db.close()
        upgraded = DeliveryStore(old_path)
        try:
            self.assertEqual(upgraded.get('key')['message_id'], '101')
            self.assertEqual(upgraded.get('key')['deleted'], 0)
        finally:
            upgraded.close()

    async def test_pause_and_resume_after_restart_do_not_repeat_sent_stream(self):
        from dataclasses import replace
        await self.monitor.check_all()
        self.assertEqual(self.channel.send_count, 1)
        stream = {**self.config.servers[0].streams[0], 'paused': True}
        self.config = replace(self.config, servers=[replace(self.config.servers[0], streams=[stream])])
        await self.monitor.apply_config(self.config)
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=self.info)
        await self.monitor.check_all()
        await self.restart()
        await self.monitor.check_all()
        self.assertEqual(self.channel.send_count, 1)
        stream['paused'] = False
        self.config = replace(self.config, servers=[replace(self.config.servers[0], streams=[stream])])
        await self.monitor.apply_config(self.config)
        self.monitor.platforms[0].get_stream_info = AsyncMock(return_value=self.info)
        await self.monitor.check_all()
        self.assertEqual(self.channel.send_count, 1)
