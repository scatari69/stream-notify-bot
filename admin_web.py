"""Local Discord-authenticated administration panel."""
import asyncio
import html
import json
import os
import secrets
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from app_paths import ROOT, CONFIG, SECRETS, STATUS
from server_settings import validate_server
from utils.config import ConfigError, load_config

ORIGIN = 'http://127.0.0.1:8765'
API = 'https://discord.com/api/v10'
KEYS = ('DISCORD_TOKEN', 'YOUTUBE_API_KEY', 'TWITCH_CLIENT_ID', 'TWITCH_CLIENT_SECRET')


def read_json(path, default):
    if not path.exists():
        return default
    with path.open(encoding='utf-8') as file:
        result = json.load(file)
    if not isinstance(result, dict):
        raise ValueError('Ожидался JSON-объект')
    return result


def atomic_write(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.admin-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def credential(name):
    return read_json(SECRETS, {}).get(name) or os.environ.get(name, '')


def page(body, status=200):
    body = f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#101114"><title>Stream Notify</title><link rel="stylesheet" href="/static/admin.css?v=2"><script src="/static/admin.js?v=2" defer></script></head><body><div class="shell"><header class="topbar"><a class="brand" href="/"><span class="brand-mark" aria-hidden="true">✳</span> Stream Notify</a><span class="top-label">Панель управления</span></header><main class="content">{body}</main></div></body></html>'''
    return web.Response(text=body, status=status, content_type='text/html', headers={
        'Cache-Control': 'no-store', 'Referrer-Policy': 'same-origin',
        'Content-Security-Policy': "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'",
        'X-Content-Type-Options': 'nosniff',
    })


def esc(value):
    return html.escape(str(value), quote=True)


def csrf(session):
    return f'<input type="hidden" name="csrf" value="{session["csrf"]}">'


@web.middleware
async def guard(request, handler):
    if request.host.lower() != urlsplit(request.app['origin']).netloc.lower():
        raise web.HTTPForbidden()
    if request.method == 'POST' and request.headers.get('Origin') != request.app['origin']:
        raise web.HTTPForbidden()
    try:
        return await handler(request)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return page('<p>Discord недоступен или отклонил запрос. Повторите попытку позже.</p>', 502)
    except (ValueError, ConfigError) as exc:
        return page(f'<p>{esc(exc)}</p><a href="/">Назад</a>', 400)


def session_for(request):
    sid = request.cookies.get('session', '')
    session = request.app['sessions'].get(sid)
    if not session or session['expires'] <= time.time():
        request.app['sessions'].pop(sid, None)
        raise web.HTTPFound('/')
    return session


async def checked_form(request, session):
    form = await request.post()
    if not secrets.compare_digest(form.get('csrf', ''), session['csrf']):
        raise web.HTTPForbidden()
    return form


async def discord_get(request, path, token, params=None):
    async with request.app['http'].get(API + path, headers={'Authorization': token}, params=params) as response:
        response.raise_for_status()
        return await response.json()


async def guilds(request, token):
    result = []
    after = '0'
    while True:
        batch = await discord_get(request, '/users/@me/guilds', token, {'limit': 200, 'after': after})
        result.extend(batch)
        if len(batch) < 200:
            return result
        after = batch[-1]['id']


async def accessible(request, session):
    user_guilds = await guilds(request, 'Bearer ' + session['token'])
    token = credential('DISCORD_TOKEN')
    if not token:
        return []
    bot_guilds = await guilds(request, 'Bot ' + token)
    bot_ids = {guild['id'] for guild in bot_guilds}
    return [guild for guild in user_guilds if guild['id'] in bot_ids and
            (guild.get('owner') or int(guild.get('permissions', '0')) & 8)]


async def authorize_guild(request, session):
    guild_id = request.match_info['guild_id']
    if not guild_id.isascii() or not guild_id.isdigit() or not any(g['id'] == guild_id for g in await accessible(request, session)):
        raise web.HTTPForbidden()
    return int(guild_id)


async def index(request):
    invite_url = 'https://discord.com/oauth2/authorize?' + urlencode({
        'client_id': request.app['client_id'], 'permissions': '216064', 'scope': 'bot applications.commands',
    })
    invite = f'<a class="button secondary" href="{esc(invite_url)}">＋ Добавить бота на сервер Discord</a>'
    session = request.app['sessions'].get(request.cookies.get('session', ''))
    if not session or session['expires'] <= time.time():
        return page(f'''<div class="hero login-hero"><div><div class="eyebrow">Всегда на связи</div><h2>Каждый эфир<br>начинается здесь.</h2><p>Управляйте стримерами и уведомлениями вашего Discord-сервера в одном месте.</p><div class="hero-actions"><a class="button" href="/login">Войти через Discord <span aria-hidden="true">↗</span></a>{invite}</div></div></div><div class="login-note"><span class="live-dot"></span> TWITCH &nbsp; / &nbsp; YOUTUBE <span>Уведомления, когда это важно.</span></div>''')
    try:
        servers = await accessible(request, session)
        error = ''
    except (aiohttp.ClientError, asyncio.TimeoutError):
        servers = []
        error = '<p class="notice">Не удалось получить серверы Discord. Проверьте токен бота и подключение.</p>'
    links = ''.join(f'<a class="server-choice" href="/servers/{esc(g["id"])}"><span class="avatar">{esc(g["name"][:1].upper())}</span><span class="server-name">{esc(g["name"])}</span><span aria-hidden="true">↗</span></a>' for g in servers)
    owner = session['user_id'] == request.app['owner_id']
    owner_link = '<a class="button ghost-link" href="/secrets">Общие токены и ключи</a>' if owner else ''
    return page(f'''<div class="hero"><div><div class="eyebrow">Рады видеть вас</div><h2>Ваши серверы</h2><p>Вы вошли как {esc(session["username"])}. Выберите сервер для управления.</p></div></div>{error}<div class="server-list">{links or '<div class="empty">Нет доступных серверов. Добавьте бота на сервер и проверьте права администратора.</div>'}</div><div class="home-actions">{invite}{owner_link}<form method="post" action="/logout">{csrf(session)}<button class="ghost" type="submit">Выйти</button></form></div>''')


async def login(request):
    now = time.time()
    for state, entry in list(request.app['states'].items()):
        if entry[1] <= now:
            del request.app['states'][state]
    for sid, session in list(request.app['sessions'].items()):
        if session['expires'] <= now:
            del request.app['sessions'][sid]
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    request.app['states'][state] = (nonce, time.time() + 300)
    url = 'https://discord.com/oauth2/authorize?' + urlencode({'client_id': request.app['client_id'], 'redirect_uri': request.app['origin'] + '/callback', 'response_type': 'code', 'scope': 'identify guilds', 'state': state})
    response = web.HTTPFound(url)
    response.set_cookie('oauth', nonce, httponly=True, secure=request.app['origin'].startswith('https://'), samesite='Lax', max_age=300, path='/')
    raise response


async def callback(request):
    state = request.query.get('state', '')
    entry = request.app['states'].pop(state, None)
    if not entry or entry[1] < time.time() or not secrets.compare_digest(entry[0], request.cookies.get('oauth', '')) or not request.query.get('code'):
        raise web.HTTPForbidden()
    async with request.app['http'].post(API + '/oauth2/token', data={
        'client_id': request.app['client_id'], 'client_secret': request.app['client_secret'],
        'grant_type': 'authorization_code', 'code': request.query['code'], 'redirect_uri': request.app['origin'] + '/callback',
    }) as response:
        response.raise_for_status()
        token = await response.json()
    user = await discord_get(request, '/users/@me', 'Bearer ' + token['access_token'])
    sid = secrets.token_urlsafe(32)
    request.app['sessions'][sid] = {'token': token['access_token'], 'user_id': user['id'], 'username': user.get('global_name') or user['username'], 'csrf': secrets.token_urlsafe(32), 'expires': time.time() + min(int(token.get('expires_in', 3600)), 28800)}
    response = web.HTTPFound('/')
    response.del_cookie('oauth', path='/')
    response.set_cookie('session', sid, httponly=True, secure=request.app['origin'].startswith('https://'), samesite='Lax', max_age=28800, path='/')
    raise response


async def logout(request):
    session = session_for(request)
    await checked_form(request, session)
    request.app['sessions'].pop(request.cookies.get('session'), None)
    response = web.HTTPFound('/')
    response.del_cookie('session', path='/')
    raise response


def selected(value, current):
    return ' selected' if str(value) == str(current) else ''


async def members_list(request, guild_id, token):
    result = []
    after = '0'
    while True:
        batch = await discord_get(request, f'/guilds/{guild_id}/members', token, {'limit': 1000, 'after': after})
        result.extend(batch)
        if len(batch) < 1000:
            return result
        after = batch[-1]['user']['id']


def server_data(guild_id):
    config = read_json(CONFIG, {'servers': []})
    server = next((s for s in config.get('servers', []) if s['guild_id'] == guild_id), None)
    return config, server


async def server_page(request):
    session = session_for(request)
    guild_id = await authorize_guild(request, session)
    config, server = server_data(guild_id)
    if not server:
        return page(f'''<a class="breadcrumb" href="/">← Выбор сервера</a><div class="hero"><div><div class="eyebrow">Новый сервер</div><h2>Всё начинается<br>с настройки.</h2><p>Выберите канал уведомлений и добавьте стримеров, чтобы бот начал следить за эфирами.</p><div class="hero-actions"><a class="button" href="/servers/{guild_id}/settings">Настроить сервер ↗</a></div></div></div>''')
    snapshot = read_json(STATUS, {})
    timestamp = snapshot.get('updated_at')
    try:
        updated = datetime.fromisoformat(timestamp)
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        fresh = 0 <= age <= max(120, int(snapshot.get('poll_interval', 60)) * 2 + 30)
    except (KeyError, TypeError, ValueError, OverflowError, ProcessLookupError):
        fresh = False
    observed = {(entry.get('platform'), (entry.get('channel') or '').lower() if entry.get('platform') == 'twitch' else entry.get('channel')): entry
                for entry in snapshot.get('streams', []) if entry.get('guild_id') == guild_id}
    rows = []
    online = 0
    errors = {
        'platform_unavailable': 'Не удалось проверить площадку; бот повторит попытку.',
        'access_denied': 'Доступ отклонён; проверьте ключ API, права бота и лимиты.',
        'rate_limited': 'Превышен лимит запросов; бот повторит попытку.',
        'channel_unavailable': 'Канал уведомлений недоступен боту.',
        'history_unavailable': 'Не удалось проверить историю канала перед повторной отправкой.',
        'send_failed': 'Не удалось подтвердить отправку уведомления; бот повторит попытку.',
        'storage_error': 'Не удалось сохранить состояние доставки; проверьте доступность диска.',
        'delete_failed': 'Не удалось удалить уведомление; бот повторит попытку.',
        'old_delivery': 'Найдена несовместимая старая запись уведомления.',
    }
    for stream in server.get('streams', []):
        platforms = []
        for key, name in (('youtube_channel_id', 'youtube'), ('twitch_username', 'twitch')):
            channel = stream.get(key, '')
            if not channel:
                continue
            item = observed.get((name, channel.lower() if name == 'twitch' else channel), {}) if fresh else {}
            state = item.get('status', 'unknown')
            label = {'online': 'В эфире', 'offline': 'Не в эфире'}.get(state, 'Нет данных')
            if state == 'online':
                online += 1
            title = f' — {esc(item["title"])}' if item.get('title') else ''
            problems = [errors[item[key]] for key in ('check_error', 'delivery_error', 'delete_error') if item.get(key) in errors]
            detail = ''.join(f'<span class="status-error">{esc(problem)}</span>' for problem in problems)
            status_class = state if state in ('online', 'offline') else 'unknown'
            platforms.append(f'<span class="platform-line"><span class="status-dot {status_class}"></span>{name.capitalize()}: {label}{title}</span>{detail}')
        linked = f'<span class="stream-meta">Discord ID · {esc(stream["discord_user_id"])}</span>' if stream.get('discord_user_id') else ''
        paused = '<span class="pause-badge">Уведомления на паузе</span>' if stream.get('paused', False) else ''
        rows.append(f'<article class="overview-row"><span class="avatar" aria-hidden="true">{esc(stream["name"][:1].upper())}</span><div><strong>{esc(stream["name"])}</strong> {paused} {linked}<div class="platforms">{"".join(platforms)}</div></div></article>')
    info = f'Последнее обновление: {esc(timestamp)}' if fresh else 'Нет свежих данных: бот ещё не проверил стримы или остановлен.'
    runtime_errors = {
        'config_error': 'Бот не смог применить настройки или прочитать ключи; работают последние корректные настройки.',
        'discord_token_invalid': 'Discord отклонил новый токен бота; используется предыдущее подключение.',
        'discord_unavailable': 'Не удалось проверить токен Discord; бот повторит попытку.',
    }
    runtime = runtime_errors.get(snapshot.get('runtime_error')) if fresh else None
    runtime_notice = f'<p><strong>Ошибка:</strong> {esc(runtime)}</p>' if runtime else ''
    return page(f'''<a class="breadcrumb" href="/">← Выбор сервера</a><div class="hero"><div><div class="eyebrow">Панель сервера</div><h2>Обзор</h2><p>Статус трансляций и уведомлений в одном месте.</p></div><a class="button secondary" href="/servers/{guild_id}/settings">⚙ Настройки</a></div><div class="stats-grid"><div class="stat-card"><span>Стримеров</span><strong>{len(server.get('streams', []))}</strong><small>Добавлено на сервере</small></div><div class="stat-card"><span>Сейчас в эфире</span><strong>{online}</strong><small>Активных площадок</small></div><div class="stat-card"><span>Уведомления</span><strong class="stat-format">{esc(server.get('message_type', 'embed')).upper()}</strong><small>Канал · {esc(server['notification_channel_id'])}</small></div></div><p class="update-time">{info}</p>{runtime_notice}<section class="panel"><div class="list-heading"><div><h3>Стримеры</h3><p>Последнее состояние отслеживаемых каналов.</p></div><a class="button secondary" href="/servers/{guild_id}/settings">Управлять ↗</a></div><div class="overview-list">{''.join(rows) or '<div class="empty">Стримеры ещё не добавлены.</div>'}</div></section>''')


async def settings_page(request):
    session = session_for(request)
    guild_id = await authorize_guild(request, session)
    config, server = server_data(guild_id)
    server = server or {'notification_channel_id': '', 'live_role_id': 0, 'streams': []}
    token = 'Bot ' + credential('DISCORD_TOKEN')
    channels, roles = await asyncio.gather(discord_get(request, f'/guilds/{guild_id}/channels', token), discord_get(request, f'/guilds/{guild_id}/roles', token))
    try:
        members = await members_list(request, guild_id, token)
        member_warning = ''
    except aiohttp.ClientResponseError as exc:
        if exc.status != 403:
            raise
        members = []
        member_warning = '<p>Список участников недоступен. Включите Server Members Intent в Discord Developer Portal; существующие привязки сохраняются.</p>'
    channel_options = ''.join(f'<option value="{esc(c["id"])}"{selected(c["id"], server["notification_channel_id"])}>#{esc(c["name"])}</option>' for c in channels if c['type'] == 0)
    role_options = '<option value="0">Без роли</option>' + ''.join(f'<option value="{esc(r["id"])}"{selected(r["id"], server.get("live_role_id", 0))}>{esc(r["name"])}</option>' for r in roles if r['id'] != str(guild_id))
    member_names = {member['user']['id']: member.get('nick') or member['user'].get('global_name') or member['user']['username'] for member in members if not member['user'].get('bot')}
    for stream in server['streams']:
        user_id = stream.get('discord_user_id', '')
        if user_id and user_id not in member_names:
            member_names[user_id] = f'Участник {user_id}'
    member_options = '<option value="">Не выбран</option>' + ''.join(
        f'<option value="{esc(user_id)}">{esc(name)}</option>' for user_id, name in member_names.items())

    def stream_row(stream):
        data = {'name': stream.get('name', ''), 'youtube': stream.get('youtube_channel_id', ''),
                'twitch': stream.get('twitch_username', ''), 'user': stream.get('discord_user_id', ''),
                'paused': '1' if stream.get('paused', False) else '0'}
        inputs = ''.join(f'<input type="hidden" name="{key}" value="{esc(value)}">' for key, value in data.items())
        details = [f'YouTube · {data["youtube"]}' if data['youtube'] else '',
                   f'Twitch · {data["twitch"]}' if data['twitch'] else '',
                   f'Discord · {member_names.get(data["user"], data["user"])}' if data['user'] else '']
        description = '   /   '.join(detail for detail in details if detail)
        return f'''<article class="stream-row" data-persisted="{'1' if stream else '0'}" data-saved-youtube="{esc(data['youtube'])}" data-saved-twitch="{esc(data['twitch'])}">{inputs}<span class="avatar" aria-hidden="true">{esc(data['name'][:1].upper() or '•')}</span><div class="stream-info"><div class="stream-name">{esc(data['name'])}</div><span class="pause-badge"{' hidden' if data['paused'] == '0' else ''}>Уведомления на паузе</span><div class="stream-meta">{esc(description)}</div></div><div class="stream-actions"><button class="ghost" type="button" data-action="pause" aria-label="{'Возобновить' if data['paused'] == '1' else 'Приостановить'} уведомления: {esc(data['name'])}" aria-pressed="{'true' if data['paused'] == '1' else 'false'}"{' disabled' if not stream else ''}>{'Возобновить' if data['paused'] == '1' else 'Пауза'}</button><button class="ghost" type="button" data-action="edit" aria-label="Изменить {esc(data['name'])}">Изменить</button><button class="danger-text" type="button" data-action="delete" aria-label="Удалить {esc(data['name'])}">Удалить</button></div><span class="pause-result" role="status" aria-live="polite"></span></article>'''

    rows = ''.join(stream_row(stream) for stream in server['streams'])
    template = stream_row({})
    message_type = server.get('message_type', 'embed')
    message = server.get('custom_message', config.get('custom_message', '**{name}** just went live on {platform}!'))
    delete = server.get('delete_after_offline', config.get('delete_after_offline', False))
    member_warning = f'<div class="notice">{member_warning}</div>' if member_warning else ''
    return page(f'''<a class="breadcrumb" href="/servers/{guild_id}">← Обзор сервера</a>
<div class="hero"><div><div class="eyebrow">Управление сервером</div><h2>Настройки</h2><p>Стримеры и уведомления — всё в одном месте.</p></div><span class="hero-side">ID сервера · {guild_id}</span></div>
{member_warning}<noscript><p class="notice">Для добавления, изменения и удаления стримеров включите JavaScript.</p></noscript>
<form method="post" id="settings-form">{csrf(session)}
<section class="panel"><div class="panel-heading"><div class="panel-title"><span class="number">01</span><div><h3>Уведомления</h3><p>Куда и как отправлять сообщения о трансляциях.</p></div></div></div>
<div class="grid"><label class="field">Канал уведомлений<select name="channel" required><option value="">Выберите канал</option>{channel_options}</select></label><label class="field">Роль для упоминания<select name="role">{role_options}</select></label>
<label class="field field-wide">Формат сообщения<select name="message_type"><option value="plain"{selected('plain', message_type)}>Простой текст</option><option value="embed"{selected('embed', message_type)}>Embed с предпросмотром</option></select></label>
<label class="field field-wide">Текст уведомления<textarea name="custom_message" maxlength="1000" required>{esc(message)}</textarea><small class="template-hint">Переменные: {{name}}, {{streamer}}, {{platform}}, {{url}}. Ссылка добавится в простой текст автоматически.</small></label>
<label class="checkbox field-wide"><input type="checkbox" name="delete_after_offline" value="1"{' checked' if delete else ''}> Удалять уведомление после окончания эфира</label></div></section>
<section class="panel"><div class="list-heading"><div class="panel-title"><span class="number">02</span><div><h3>Стримеры</h3><p>Пауза отключает уведомления, но сохраняет статус. После возобновления текущий эфир может вызвать уведомление.</p></div></div><button class="secondary" type="button" id="add-stream">＋ Добавить стримера</button></div>
<div id="stream-list" class="settings-list" data-pause-url="/servers/{guild_id}/pause">{rows}</div><div id="stream-empty" class="empty"{' hidden' if rows else ''}>Пока нет стримеров. Добавьте первого, чтобы начать отслеживание.</div></section>
<div class="savebar"><p id="save-hint" role="status">Изменения применяются после сохранения.</p><button type="submit">Сохранить настройки <span aria-hidden="true">↗</span></button></div></form>
<template id="stream-template">{template}</template>
<dialog id="stream-dialog" aria-labelledby="stream-dialog-title"><form id="stream-editor" method="dialog" class="dialog-inner"><div class="dialog-head"><div><h2 id="stream-dialog-title">Добавить стримера</h2><p>Укажите хотя бы одну платформу для отслеживания.</p></div><button class="icon" type="button" data-close-dialog aria-label="Закрыть">×</button></div><div class="dialog-fields"><label>Имя стримера<input name="name" maxlength="100" required placeholder="Например, Alex"></label><label>Канал YouTube<input name="youtube" maxlength="100" placeholder="ID канала, начинается с UC"></label><label>Логин Twitch<input name="twitch" maxlength="100" placeholder="Например, twitch_login"></label><label>Участник Discord<select name="user">{member_options}</select><small>Связанный участник будет упомянут через {{streamer}}.</small></label></div><div class="dialog-actions"><button type="button" class="ghost" data-close-dialog>Отмена</button><button type="submit">Готово</button></div></form></dialog>
<dialog id="delete-dialog" aria-labelledby="delete-title"><div class="dialog-inner"><div class="dialog-head"><div><h2 id="delete-title">Удалить стримера?</h2><p>Бот перестанет отслеживать <strong id="delete-name"></strong> после сохранения настроек.</p></div><button class="icon" type="button" data-close-dialog aria-label="Закрыть">×</button></div><div class="dialog-actions"><button type="button" class="ghost" data-close-dialog>Отмена</button><button type="button" class="danger" id="confirm-delete">Удалить</button></div></div></dialog>''')


async def save_server(request):
    session = session_for(request)
    form = await checked_form(request, session)
    guild_id = await authorize_guild(request, session)
    channel, role = form.get('channel', ''), form.get('role', '')
    if not all(value.isascii() and value.isdigit() and len(value) <= 20 for value in (channel, role)):
        raise ValueError('Неверный ID канала или роли')
    names, youtube, twitch, users, paused = (form.getall(key, []) for key in ('name', 'youtube', 'twitch', 'user', 'paused'))
    if not len(names) == len(youtube) == len(twitch) == len(users) == len(paused) or len(names) > 101 or any(value not in ('0', '1') for value in paused):
        raise ValueError('Неверный список стримеров')
    deleted = set(form.getall('delete', []))
    streams = []
    for index, (name, yt, tw, user, pause) in enumerate(zip(names, youtube, twitch, users, paused)):
        if str(index) in deleted:
            continue
        name, yt, tw, user = name.strip(), yt.strip(), tw.strip(), user.strip()
        if name or yt or tw or user:
            streams.append({'name': name, 'youtube_channel_id': yt, 'twitch_username': tw, 'discord_user_id': user, 'paused': pause == '1'})
    server = {'guild_id': guild_id, 'notification_channel_id': int(channel), 'live_role_id': int(role), 'streams': streams,
              'message_type': form.get('message_type', ''), 'custom_message': form.get('custom_message', '').strip(),
              'delete_after_offline': form.get('delete_after_offline') == '1'}
    validate_server(server)
    token = 'Bot ' + credential('DISCORD_TOKEN')
    channels, roles = await asyncio.gather(discord_get(request, f'/guilds/{guild_id}/channels', token), discord_get(request, f'/guilds/{guild_id}/roles', token))
    if not any(int(c['id']) == server['notification_channel_id'] and c['type'] == 0 for c in channels):
        raise ValueError('Текстовый канал не найден на этом сервере')
    if server['live_role_id'] and not any(int(r['id']) == server['live_role_id'] for r in roles):
        raise ValueError('Роль не найдена на этом сервере')
    for user_id in {stream['discord_user_id'] for stream in streams if stream['discord_user_id']}:
        member = await discord_get(request, f'/guilds/{guild_id}/members/{user_id}', token)
        if member.get('user', {}).get('id') != user_id or member['user'].get('bot'):
            raise ValueError('Выбранный пользователь не является участником сервера')
    async with request.app['lock']:
        config = read_json(CONFIG, {'servers': []})
        config['servers'] = [s for s in config.get('servers', []) if s['guild_id'] != guild_id] + [server]
        fd, temporary = tempfile.mkstemp(dir=CONFIG.parent, prefix='.admin-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                json.dump(config, file)
            load_config(temporary, ROOT / '.env')
            os.replace(temporary, CONFIG)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return page(f'<p>Сохранено. Бот применит настройки в течение нескольких секунд.</p><a href="/servers/{guild_id}">К обзору сервера</a>')


async def pause_stream(request):
    session = session_for(request)
    form = await checked_form(request, session)
    guild_id = await authorize_guild(request, session)
    youtube, twitch, paused = form.get('youtube', ''), form.get('twitch', ''), form.get('paused', '')
    if not (youtube or twitch) or len(youtube) > 100 or len(twitch) > 100 or paused not in ('0', '1'):
        raise ValueError('Неверные параметры стримера')
    async with request.app['lock']:
        config, server = server_data(guild_id)
        if not server:
            raise web.HTTPConflict(text='Настройки сервера изменились')
        stream = next((s for s in server['streams'] if s.get('youtube_channel_id', '') == youtube and s.get('twitch_username', '') == twitch), None)
        if stream is None:
            raise web.HTTPConflict(text='Стример изменён или удалён')
        stream['paused'] = paused == '1'
        validate_server(server)
        atomic_write(CONFIG, config)
    return web.json_response({'paused': stream['paused']}, headers={'Cache-Control': 'no-store'})


def require_owner(request):
    session = session_for(request)
    if session['user_id'] != request.app['owner_id']:
        raise web.HTTPForbidden()
    return session


async def secrets_page(request):
    session = require_owner(request)
    fields = ''.join(f'<label>{key}<input name="{key}" type="password" autocomplete="new-password" placeholder="Оставьте пустым, чтобы не менять"></label>' for key in KEYS)
    return page(f'<p><a href="/">← Серверы</a></p><h2>Общие токены и ключи</h2><p>Сохранённые значения не отображаются. Изменения применяются автоматически; новый токен Discord должен принадлежать тому же боту.</p><form method="post" class="panel dialog-fields">{csrf(session)}{fields}<button>Сохранить</button></form>')


async def save_secrets(request):
    session = require_owner(request)
    form = await checked_form(request, session)
    updates = {key: form.get(key, '').strip() for key in KEYS if form.get(key, '').strip()}
    if any(len(value) > 2048 or '\n' in value or '\r' in value for value in updates.values()):
        raise ValueError('Неверный формат ключа')
    if updates.get('DISCORD_TOKEN'):
        bot = await discord_get(request, '/users/@me', 'Bot ' + updates['DISCORD_TOKEN'])
        if bot.get('id') != request.app['client_id'] or not bot.get('bot'):
            raise ValueError('Токен Discord должен принадлежать этому приложению')
    async with request.app['lock']:
        current = read_json(SECRETS, {})
        current.update(updates)
        atomic_write(SECRETS, current)
    return page('<p>Ключи сохранены. Бот применит изменения автоматически.</p><a href="/">К серверам</a>')


def validate_origin(origin):
    parsed = urlsplit(origin)
    if (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or parsed.netloc != parsed.netloc.lower()):
        raise ValueError('PUBLIC_BASE_URL должен быть адресом без пути, например https://bot.example.com')
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError('Неверный порт PUBLIC_BASE_URL') from exc
    if parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('Для внешнего адреса PUBLIC_BASE_URL необходим HTTPS')
    return origin


async def create_app(client_id, client_secret, owner_id, origin=ORIGIN):
    origin = validate_origin(origin)
    app = web.Application(middlewares=[guard], client_max_size=65536)
    app['client_id'], app['client_secret'], app['owner_id'], app['origin'] = client_id, client_secret, owner_id, origin
    app['sessions'], app['states'], app['lock'] = {}, {}, asyncio.Lock()
    async def start(app):
        app['http'] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    async def stop(app):
        await app['http'].close()
    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    app.router.add_static('/static/', ROOT / 'static')
    app.add_routes([web.get('/', index), web.get('/login', login), web.get('/callback', callback), web.post('/logout', logout), web.get('/servers/{guild_id}', server_page), web.get('/servers/{guild_id}/settings', settings_page), web.post('/servers/{guild_id}/settings', save_server), web.post('/servers/{guild_id}/pause', pause_stream), web.get('/secrets', secrets_page), web.post('/secrets', save_secrets)])
    return app


if __name__ == '__main__':
    load_dotenv(ROOT / '.env')
    client_id, client_secret, owner_id = (os.getenv(key, '') for key in ('DISCORD_CLIENT_ID', 'DISCORD_CLIENT_SECRET', 'DISCORD_OWNER_ID'))
    if not client_id.isascii() or not client_id.isdigit() or not client_secret or not owner_id.isascii() or not owner_id.isdigit():
        raise SystemExit('Заполните DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET и DISCORD_OWNER_ID в .env')
    origin = os.getenv('PUBLIC_BASE_URL', ORIGIN)
    web.run_app(create_app(client_id, client_secret, owner_id, origin), host=os.getenv('WEB_BIND_HOST', '127.0.0.1'), port=8765, access_log=None)
