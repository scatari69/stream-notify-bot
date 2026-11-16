import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from utils.config import ConfigError, load_config


class ConfigServerTests(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps(data))
            with patch.dict(os.environ, {'DISCORD_TOKEN': 'test'}, clear=True):
                return load_config(path, Path(folder) / '.env')

    def test_starts_before_server_configuration_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {'DISCORD_TOKEN': 'test'}, clear=True):
                config = load_config(Path(folder) / 'config.json', Path(folder) / '.env', allow_missing=True)
        self.assertEqual(config.servers, [])

    def test_missing_configuration_is_rejected_after_setup(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {'DISCORD_TOKEN': 'test'}, clear=True):
                with self.assertRaises(ConfigError):
                    load_config(Path(folder) / 'config.json', Path(folder) / '.env')

    def test_loads_independent_servers(self):
        config = self.load({'servers': [
            {'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'A', 'youtube_channel_id': 'UC1'}]},
            {'guild_id': 3, 'notification_channel_id': 4, 'streams': [{'name': 'B', 'youtube_channel_id': 'UC2'}]},
        ]})
        self.assertEqual(config.servers[0].streams[0]['youtube_channel_id'], 'UC1')
        self.assertEqual(config.servers[1].streams[0]['youtube_channel_id'], 'UC2')

    def test_old_format_is_not_accepted(self):
        with self.assertRaises(ConfigError):
            self.load({'servers': [], 'guild_id': 1, 'notification_channel_id': 2, 'youtube_channel_id': 'UC1'})

    def test_invalid_server_rejected(self):
        with self.assertRaises(ConfigError):
            self.load({'youtube_channel_id': 'UC1', 'servers': [{'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'A'}]}]})

    def test_removed_webhook_mode_is_not_silently_used(self):
        with self.assertRaisesRegex(ConfigError, 'removed'):
            self.load({'webhook_mode': True, 'servers': []})


if __name__ == '__main__':
    unittest.main()
