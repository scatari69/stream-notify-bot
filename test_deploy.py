import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class DeploymentPathsTests(unittest.TestCase):
    def test_shared_directory_is_used_by_panel_and_bot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config.json').write_text(json.dumps({'servers': [{'guild_id': 1, 'notification_channel_id': 2, 'streams': []}]}))
            (root / '.secrets.json').write_text(json.dumps({'DISCORD_TOKEN': 'private-token'}))
            env = {**os.environ, 'APP_DATA_DIR': folder, 'DISCORD_TOKEN': ''}
            code = '''from app_paths import CONFIG, SECRETS, STATUS, DELIVERIES
import admin_web
from utils.config import load_config
assert admin_web.CONFIG == CONFIG and admin_web.SECRETS == SECRETS and admin_web.STATUS == STATUS
assert CONFIG.parent == SECRETS.parent == STATUS.parent == DELIVERIES.parent
assert load_config(CONFIG).discord_token == "private-token"
'''
            subprocess.run([sys.executable, '-B', '-c', code], env=env, check=True, capture_output=True)
