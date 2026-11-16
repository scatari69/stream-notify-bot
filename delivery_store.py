"""Durable delivery records for notifications and uncertain sends."""
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path


class DeliveryStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.execute('''CREATE TABLE IF NOT EXISTS deliveries (
            stream_key TEXT PRIMARY KEY, marker TEXT NOT NULL, channel_id TEXT NOT NULL,
            author_id TEXT NOT NULL, is_webhook INTEGER NOT NULL, started REAL NOT NULL,
            message_id TEXT
        )''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(deliveries)')}
        if 'deleted' not in columns:
            self.db.execute('ALTER TABLE deliveries ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0')
        self.db.commit()

    @staticmethod
    def key(platform, info):
        config = platform.config
        channel = config.youtube_channel_id if platform.name == 'youtube' else config.twitch_username.lower()
        return json.dumps([str(config.servers[0].guild_id), platform.name, channel, info.unique_key], ensure_ascii=False)

    def get(self, key):
        row = self.db.execute('SELECT marker, channel_id, author_id, is_webhook, started, message_id, deleted FROM deliveries WHERE stream_key = ?', (key,)).fetchone()
        if row is None:
            return None
        return dict(zip(('marker', 'channel_id', 'author_id', 'is_webhook', 'started', 'message_id', 'deleted'), row))

    def begin(self, key, channel_id, author_id):
        marker = 'sn:' + secrets.token_hex(12)
        with self.db:
            self.db.execute('INSERT INTO deliveries (stream_key, marker, channel_id, author_id, is_webhook, started, message_id) VALUES (?, ?, ?, ?, 0, ?, NULL)',
                            (key, marker, str(channel_id), str(author_id), time.time() - 30))
        return self.get(key)

    def retarget(self, key, channel_id, author_id):
        with self.db:
            self.db.execute('UPDATE deliveries SET channel_id = ?, author_id = ?, is_webhook = 0, started = ? WHERE stream_key = ?',
                            (str(channel_id), str(author_id), time.time() - 30, key))

    def sent(self, key, channel_id, message_id):
        with self.db:
            self.db.execute('UPDATE deliveries SET channel_id = ?, message_id = ?, deleted = 0 WHERE stream_key = ?', (str(channel_id), str(message_id), key))

    def outstanding(self, platform):
        guild, name, channel = platform.config.servers[0].guild_id, platform.name, (platform.config.youtube_channel_id if platform.name == 'youtube' else platform.config.twitch_username.lower())
        entries = self.db.execute('SELECT stream_key FROM deliveries WHERE deleted = 0 AND is_webhook = 0').fetchall()
        result = []
        for (key,) in entries:
            parts = json.loads(key)
            if parts[:3] == [str(guild), name, channel]:
                result.append((key, parts[3], self.get(key)))
        return result

    def mark_deleted(self, key):
        with self.db:
            self.db.execute('UPDATE deliveries SET deleted = 1 WHERE stream_key = ?', (key,))

    def delete(self, key):
        with self.db:
            self.db.execute('DELETE FROM deliveries WHERE stream_key = ?', (key,))

    def close(self):
        self.db.close()
