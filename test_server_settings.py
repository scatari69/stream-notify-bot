import unittest
from server_settings import validate_server


class ServerSettingsTests(unittest.TestCase):
    def test_rejects_invalid_and_duplicate_channels(self):
        server = {'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'A', 'twitch_username': 'Test'}, {'name': 'B', 'twitch_username': 'test'}]}
        with self.assertRaises(ValueError):
            validate_server(server)
        server['streams'].pop()
        server['guild_id'] = True
        with self.assertRaises(ValueError):
            validate_server(server)

    def test_rejects_bad_message_format_and_user_id(self):
        server = {'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'A', 'youtube_channel_id': 'UC1', 'discord_user_id': '5'}], 'message_type': 'plain', 'custom_message': 'Live {name}'}
        validate_server(server)
        server['custom_message'] = '{name.__class__}'
        with self.assertRaises(ValueError):
            validate_server(server)
        server['custom_message'] = 'Live'
        server['streams'][0]['discord_user_id'] = 'not-an-id'
        with self.assertRaises(ValueError):
            validate_server(server)

    def test_pause_must_be_boolean(self):
        server = {'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'A', 'youtube_channel_id': 'UC1', 'paused': True}]}
        validate_server(server)
        server['streams'][0]['paused'] = 'true'
        with self.assertRaises(ValueError):
            validate_server(server)
