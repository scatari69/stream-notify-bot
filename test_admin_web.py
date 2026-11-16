import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
import admin_web


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.patches = [patch.dict(os.environ, {'DISCORD_TOKEN': 'bot'}), patch.object(admin_web, 'CONFIG', Path(self.folder.name) / 'config.json'), patch.object(admin_web, 'SECRETS', Path(self.folder.name) / '.secrets.json'), patch.object(admin_web, 'STATUS', Path(self.folder.name) / 'status.json')]
        for item in self.patches:
            item.start()
        self.app = await admin_web.create_app('123', 'secret', 'owner')
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.app['sessions']['session-id'] = {'user_id': 'admin', 'username': 'Tester', 'token': 'user-token', 'csrf': 'nonce', 'expires': time.time() + 60}
        self.headers = {'Host': '127.0.0.1:8765', 'Origin': admin_web.ORIGIN, 'Cookie': 'session=session-id'}

    async def asyncTearDown(self):
        await self.client.close()
        for item in self.patches:
            item.stop()
        self.folder.cleanup()

    async def discord_get(self, request, path, token, params=None):
        if path == '/users/@me':
            return {'id': '123', 'bot': True}
        if path == '/users/@me/guilds':
            return [{'id': '1', 'name': 'Allowed', 'permissions': '8'}] if token.startswith('Bearer') else [{'id': '1'}]
        if path == '/guilds/1/channels':
            return [{'id': '2', 'type': 0, 'name': 'streams'}]
        if path == '/guilds/1/members':
            return [{'user': {'id': '1234', 'username': 'Alice'}}]
        if path == '/guilds/1/members/1234':
            return {'user': {'id': '1234', 'username': 'Alice'}}
        if path == '/guilds/1/roles':
            return []
        raise AssertionError(path)

    async def test_access_and_secrets_restricted(self):
        with patch.object(admin_web, 'discord_get', self.discord_get), patch.object(admin_web, 'credential', return_value='bot'):
            response = await self.client.get('/servers/1', headers=self.headers)
            self.assertEqual(response.status, 200)
            response = await self.client.get('/servers/3', headers=self.headers, allow_redirects=False)
            self.assertEqual(response.status, 403)
            response = await self.client.get('/secrets', headers=self.headers)
            self.assertEqual(response.status, 403)
            response = await self.client.post('/servers/1/settings', headers={**self.headers, 'Origin': 'http://evil'}, data={})
            self.assertEqual(response.status, 403)

    async def test_saves_only_authorized_server_and_checks_channel(self):
        admin_web.atomic_write(admin_web.CONFIG, {'servers': [{'guild_id': 3, 'notification_channel_id': 4, 'streams': []}]})
        form = {'csrf': 'nonce', 'channel': '2', 'role': '0', 'name': 'A', 'youtube': 'UC1', 'twitch': '', 'user': '1234', 'paused': '0', 'message_type': 'plain', 'custom_message': 'Live {name}', 'delete_after_offline': '1'}
        with patch.object(admin_web, 'discord_get', self.discord_get), patch.object(admin_web, 'credential', return_value='bot'):
            response = await self.client.post('/servers/1/settings', headers=self.headers, data=form)
            self.assertEqual(response.status, 200)
            saved = admin_web.read_json(admin_web.CONFIG, {})['servers']
            self.assertEqual([s['guild_id'] for s in saved], [3, 1])
            self.assertEqual(saved[1]['streams'][0]['discord_user_id'], '1234')
            self.assertEqual(saved[1]['message_type'], 'plain')
            self.assertTrue(saved[1]['delete_after_offline'])
            response = await self.client.post('/servers/1/settings', headers=self.headers, data={**form, 'channel': '4'})
            self.assertEqual(response.status, 400)
            response = await self.client.post('/servers/1/settings', headers=self.headers, data={**form, 'csrf': 'wrong'})
            self.assertEqual(response.status, 403)

    async def test_oauth_rejects_unbound_state(self):
        response = await self.client.get('/callback?state=wrong&code=code', headers=self.headers)
        self.assertEqual(response.status, 403)

    async def test_owner_can_set_secrets_without_disclosing_them(self):
        self.app['sessions']['session-id']['user_id'] = 'owner'
        with patch.object(admin_web, 'discord_get', self.discord_get):
            response = await self.client.post('/secrets', headers=self.headers, data={'csrf': 'nonce', 'DISCORD_TOKEN': 'sensitive'})
        self.assertEqual(response.status, 200)
        response = await self.client.get('/secrets', headers=self.headers)
        self.assertNotIn('sensitive', await response.text())
        self.assertEqual(admin_web.read_json(admin_web.SECRETS, {})['DISCORD_TOKEN'], 'sensitive')
        self.assertEqual(admin_web.SECRETS.stat().st_mode & 0o777, 0o600)

    async def test_rejects_token_for_another_application(self):
        from unittest.mock import AsyncMock
        self.app['sessions']['session-id']['user_id'] = 'owner'
        with patch.object(admin_web, 'discord_get', AsyncMock(return_value={'id': '999', 'bot': True})):
            response = await self.client.post('/secrets', headers=self.headers, data={'csrf': 'nonce', 'DISCORD_TOKEN': 'wrong'})
        self.assertEqual(response.status, 400)
        self.assertFalse(admin_web.SECRETS.exists())

    async def test_oauth_login_creates_session_and_state_cannot_be_reused(self):
        from urllib.parse import parse_qs, urlparse
        from unittest.mock import AsyncMock, MagicMock

        login = await self.client.get('/login', headers={'Host': '127.0.0.1:8765'}, allow_redirects=False)
        state = parse_qs(urlparse(login.headers['Location']).query)['state'][0]
        nonce = login.cookies['oauth'].value
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = AsyncMock(return_value={'access_token': 'oauth-token', 'expires_in': 3600})
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=None)
        headers = {'Host': '127.0.0.1:8765', 'Cookie': f'oauth={nonce}'}
        with patch.object(self.app['http'], 'post', return_value=context), patch.object(admin_web, 'discord_get', AsyncMock(return_value={'id': 'owner', 'username': 'User'})):
            callback = await self.client.get(f'/callback?state={state}&code=once', headers=headers, allow_redirects=False)
            self.assertEqual(callback.status, 302)
            sid = callback.cookies['session'].value
            self.assertEqual(self.app['sessions'][sid]['user_id'], 'owner')
            repeated = await self.client.get(f'/callback?state={state}&code=once', headers=headers, allow_redirects=False)
            self.assertEqual(repeated.status, 403)

    async def test_server_status_and_settings_show_only_current_server(self):
        from datetime import datetime, timezone
        admin_web.atomic_write(admin_web.CONFIG, {'servers': [
            {'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'Alice', 'youtube_channel_id': 'UC1', 'discord_user_id': '1234'}]},
            {'guild_id': 3, 'notification_channel_id': 4, 'streams': [{'name': 'Secret', 'youtube_channel_id': 'UC2'}]},
        ]})
        admin_web.atomic_write(admin_web.STATUS, {'pid': os.getpid(), 'updated_at': datetime.now(timezone.utc).isoformat(), 'poll_interval': 60, 'streams': [
            {'guild_id': 1, 'platform': 'youtube', 'channel': 'UC1', 'status': 'online', 'title': 'Live'},
            {'guild_id': 3, 'platform': 'youtube', 'channel': 'UC2', 'status': 'online', 'title': 'Hidden'},
        ]})
        with patch.object(admin_web, 'discord_get', self.discord_get), patch.object(admin_web, 'credential', return_value='bot'):
            response = await self.client.get('/servers/1', headers=self.headers)
            body = await response.text()
            self.assertIn('В эфире', body)
            self.assertIn('Alice', body)
            self.assertNotIn('Hidden', body)
            response = await self.client.get('/servers/1/settings', headers=self.headers)
            body = await response.text()
            self.assertEqual(response.status, 200)
            self.assertIn('#streams', body)
            self.assertIn('Alice', body)
            self.assertIn('1234', body)
            response = await self.client.get('/servers/3/settings', headers=self.headers)
            self.assertEqual(response.status, 403)

    async def test_stale_status_is_not_shown_as_live(self):
        from datetime import datetime, timezone, timedelta
        admin_web.atomic_write(admin_web.CONFIG, {'servers': [{'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'Alice', 'youtube_channel_id': 'UC1'}]}]})
        admin_web.atomic_write(admin_web.STATUS, {'pid': os.getpid(), 'updated_at': (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), 'streams': [{'guild_id': 1, 'platform': 'youtube', 'channel': 'UC1', 'status': 'online'}]})
        with patch.object(admin_web, 'discord_get', self.discord_get), patch.object(admin_web, 'credential', return_value='bot'):
            response = await self.client.get('/servers/1', headers=self.headers)
        body = await response.text()
        self.assertIn('Нет свежих данных', body)
        self.assertNotIn('YouTube: В эфире', body)

    async def test_https_proxy_origin_and_secure_oauth_cookie(self):
        from urllib.parse import parse_qs, urlparse
        app = await admin_web.create_app('123', 'secret', 'owner', 'https://stream.example.com')
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get('/login', headers={'Host': 'stream.example.com'}, allow_redirects=False)
            self.assertEqual(response.status, 302)
            self.assertTrue(response.cookies['oauth']['secure'])
            query = parse_qs(urlparse(response.headers['Location']).query)
            self.assertEqual(query['redirect_uri'], ['https://stream.example.com/callback'])
            denied = await client.get('/', headers={'Host': 'attacker.example'}, allow_redirects=False)
            self.assertEqual(denied.status, 403)
            denied = await client.post('/logout', headers={'Host': 'stream.example.com', 'Origin': 'https://attacker.example'}, allow_redirects=False)
            self.assertEqual(denied.status, 403)
        finally:
            await client.close()

    def test_public_origin_requires_https_and_no_path(self):
        self.assertEqual(admin_web.validate_origin('https://stream.example.com'), 'https://stream.example.com')
        for url in ('http://stream.example.com', 'https://stream.example.com/path', 'https://user@stream.example.com', 'https://stream.example.com/?q=1'):
            with self.assertRaises(ValueError):
                admin_web.validate_origin(url)

    async def test_dashboard_shows_errors_only_for_authorized_server(self):
        from datetime import datetime, timezone
        admin_web.atomic_write(admin_web.CONFIG, {'servers': [{'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'Alice', 'youtube_channel_id': 'UC1'}]}]})
        admin_web.atomic_write(admin_web.STATUS, {'updated_at': datetime.now(timezone.utc).isoformat(), 'streams': [
            {'guild_id': 1, 'platform': 'youtube', 'channel': 'UC1', 'status': 'unknown', 'check_error': 'access_denied', 'delivery_error': 'history_unavailable', 'delete_error': 'delete_failed'},
            {'guild_id': 3, 'platform': 'youtube', 'channel': 'UC2', 'status': 'unknown', 'check_error': 'platform_unavailable'},
        ]})
        with patch.object(admin_web, 'discord_get', self.discord_get), patch.object(admin_web, 'credential', return_value='bot'):
            response = await self.client.get('/servers/1', headers=self.headers)
        body = await response.text()
        self.assertIn('Доступ отклонён', body)
        self.assertIn('историю канала', body)
        self.assertIn('Не удалось удалить уведомление', body)
        self.assertNotIn('Не удалось проверить площадку', body)

    async def test_dashboard_shows_unapplied_configuration_error(self):
        from datetime import datetime, timezone
        admin_web.atomic_write(admin_web.CONFIG, {'servers': [{'guild_id': 1, 'notification_channel_id': 2, 'streams': []}]})
        admin_web.atomic_write(admin_web.STATUS, {'updated_at': datetime.now(timezone.utc).isoformat(), 'runtime_error': 'config_error', 'streams': []})
        with patch.object(admin_web, 'discord_get', self.discord_get), patch.object(admin_web, 'credential', return_value='bot'):
            response = await self.client.get('/servers/1', headers=self.headers)
        self.assertIn('не смог применить настройки', await response.text())

    async def test_homepage_has_bot_invite_for_guests_and_admins(self):
        from html import unescape
        for headers in ({'Host': '127.0.0.1:8765'}, self.headers):
            response = await self.client.get('/', headers=headers)
            body = unescape(await response.text())
            self.assertIn('Добавить бота на сервер Discord', body)
            self.assertIn('client_id=123&permissions=216064&scope=bot+applications.commands', body)

    async def test_forms_keep_same_origin_header_without_leaking_cross_origin_referrer(self):
        with patch.object(admin_web, 'discord_get', self.discord_get), patch.object(admin_web, 'credential', return_value='bot'):
            response = await self.client.get('/servers/1/settings', headers=self.headers)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers['Referrer-Policy'], 'same-origin')
            response = await self.client.post('/servers/1/settings', headers={**self.headers, 'Origin': 'null'}, data={'csrf': 'nonce'})
            self.assertEqual(response.status, 403)

    async def test_stream_management_renders_dialogs_and_preserves_escaped_data(self):
        name = '<script>alert(1)</script>'
        admin_web.atomic_write(admin_web.CONFIG, {'servers': [{'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': name, 'youtube_channel_id': 'UC1', 'discord_user_id': '1234'}]}]})
        with patch.object(admin_web, 'discord_get', self.discord_get), patch.object(admin_web, 'credential', return_value='bot'):
            response = await self.client.get('/servers/1/settings', headers=self.headers)
        body = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn('id="stream-dialog"', body)
        self.assertIn('id="delete-dialog"', body)
        self.assertIn('data-action="edit"', body)
        self.assertIn('data-action="delete"', body)
        self.assertIn('value="&lt;script&gt;alert(1)&lt;/script&gt;"', body)
        self.assertNotIn('<script>alert(1)</script>', body)
        self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
        self.assertIn("connect-src 'self'", response.headers['Content-Security-Policy'])
        for path in ('/static/admin.css', '/static/admin.js'):
            asset = await self.client.get(path, headers=self.headers)
            self.assertEqual(asset.status, 200)

    async def test_saving_an_empty_stream_list(self):
        admin_web.atomic_write(admin_web.CONFIG, {'servers': [{'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'A', 'youtube_channel_id': 'UC1'}]}]})
        form = {'csrf': 'nonce', 'channel': '2', 'role': '0', 'message_type': 'plain', 'custom_message': 'Live {name}'}
        with patch.object(admin_web, 'discord_get', self.discord_get), patch.object(admin_web, 'credential', return_value='bot'):
            response = await self.client.post('/servers/1/settings', headers=self.headers, data=form)
        self.assertEqual(response.status, 200)
        self.assertEqual(admin_web.read_json(admin_web.CONFIG, {})['servers'][0]['streams'], [])

    async def test_pause_updates_only_selected_stream_and_preserves_other_settings(self):
        config = {'servers': [
            {'guild_id': 1, 'notification_channel_id': 2, 'custom_message': 'Original', 'streams': [
                {'name': 'Alice', 'youtube_channel_id': 'UC1', 'twitch_username': '', 'discord_user_id': '1234'},
                {'name': 'Other', 'youtube_channel_id': 'UC2', 'twitch_username': ''}]},
            {'guild_id': 3, 'notification_channel_id': 4, 'streams': [{'name': 'Private', 'youtube_channel_id': 'UC1'}]},
        ]}
        admin_web.atomic_write(admin_web.CONFIG, config)
        body = {'csrf': 'nonce', 'youtube': 'UC1', 'twitch': '', 'paused': '1'}
        with patch.object(admin_web, 'discord_get', self.discord_get), patch.object(admin_web, 'credential', return_value='bot'):
            response = await self.client.post('/servers/1/pause', headers=self.headers, data=body)
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.json())['paused'])
            saved = admin_web.read_json(admin_web.CONFIG, {})
            self.assertTrue(saved['servers'][0]['streams'][0]['paused'])
            self.assertEqual(saved['servers'][0]['custom_message'], 'Original')
            self.assertEqual(saved['servers'][0]['streams'][1], config['servers'][0]['streams'][1])
            self.assertEqual(saved['servers'][1], config['servers'][1])
            response = await self.client.post('/servers/1/pause', headers=self.headers, data={**body, 'paused': '0'})
            self.assertEqual(response.status, 200)
            self.assertFalse((await response.json())['paused'])

    async def test_pause_checks_csrf_permissions_and_stream_identity(self):
        config = {'servers': [{'guild_id': 1, 'notification_channel_id': 2, 'streams': [{'name': 'Alice', 'youtube_channel_id': 'UC1'}]}]}
        admin_web.atomic_write(admin_web.CONFIG, config)
        body = {'csrf': 'nonce', 'youtube': 'UC1', 'twitch': '', 'paused': '1'}
        with patch.object(admin_web, 'discord_get', self.discord_get), patch.object(admin_web, 'credential', return_value='bot'):
            response = await self.client.post('/servers/1/pause', headers=self.headers, data={**body, 'csrf': 'wrong'})
            self.assertEqual(response.status, 403)
            response = await self.client.post('/servers/3/pause', headers=self.headers, data=body)
            self.assertEqual(response.status, 403)
            response = await self.client.post('/servers/1/pause', headers=self.headers, data={**body, 'youtube': 'wrong'})
            self.assertEqual(response.status, 409)
            response = await self.client.post('/servers/1/pause', headers=self.headers, data={**body, 'paused': 'yes'})
            self.assertEqual(response.status, 400)
        self.assertEqual(admin_web.read_json(admin_web.CONFIG, {}), config)

    async def test_pause_is_preserved_when_saving_settings(self):
        form = {'csrf': 'nonce', 'channel': '2', 'role': '0', 'name': 'A', 'youtube': 'UC1', 'twitch': '', 'user': '', 'paused': '1', 'message_type': 'plain', 'custom_message': 'Live {name}'}
        with patch.object(admin_web, 'discord_get', self.discord_get), patch.object(admin_web, 'credential', return_value='bot'):
            response = await self.client.post('/servers/1/settings', headers=self.headers, data=form)
        self.assertEqual(response.status, 200)
        self.assertTrue(admin_web.read_json(admin_web.CONFIG, {})['servers'][0]['streams'][0]['paused'])
