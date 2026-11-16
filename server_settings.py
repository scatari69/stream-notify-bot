import string

"""Validation for per-server stream settings."""


def validate_server(server):
    """Validate one server's delivery IDs and independent list of streamers."""
    if not isinstance(server, dict):
        raise ValueError('Server must be an object')
    for key in ('guild_id', 'notification_channel_id', 'live_role_id'):
        value = server.get(key, 0)
        if type(value) is not int or value < (0 if key == 'live_role_id' else 1) or value >= 2**64:
            raise ValueError(f'Invalid {key}')
    if server.get('message_type', 'embed') not in ('plain', 'embed'):
        raise ValueError('Invalid message type')
    if type(server.get('delete_after_offline', False)) is not bool:
        raise ValueError('Invalid delete setting')
    message = server.get('custom_message', '**{name}** just went live on {platform}!')
    if not isinstance(message, str) or not message.strip() or len(message) > 1000:
        raise ValueError('Invalid custom message')
    try:
        for _, name, spec, conversion in string.Formatter().parse(message):
            if name is not None and (name not in ('name', 'streamer', 'platform', 'url') or spec or conversion):
                raise ValueError('Invalid message placeholder')
    except ValueError as exc:
        raise ValueError('Invalid message placeholder') from exc
    streams = server.get('streams')
    if not isinstance(streams, list):
        raise ValueError('streams must be a list')
    seen = set()
    for stream in streams:
        if not isinstance(stream, dict):
            raise ValueError('Stream must be an object')
        for key in ('name', 'youtube_channel_id', 'twitch_username'):
            if not isinstance(stream.get(key, ''), str) or len(stream.get(key, '')) > 100:
                raise ValueError(f'Invalid {key}')
        if type(stream.get('paused', False)) is not bool:
            raise ValueError('Invalid pause setting')
        user_id = stream.get('discord_user_id', '')
        if not isinstance(user_id, str) or (user_id and (not user_id.isascii() or not user_id.isdigit() or not 0 < int(user_id) < 2**64)):
            raise ValueError('Invalid Discord user ID')
        if not stream.get('name', '').strip():
            raise ValueError('Streamer name is required')
        platforms = [(key, stream.get(key, '').strip().lower() if key == 'twitch_username' else stream.get(key, '').strip())
                     for key in ('youtube_channel_id', 'twitch_username') if stream.get(key, '').strip()]
        if not platforms or any(item in seen for item in platforms):
            raise ValueError('Each streamer needs a unique platform channel')
        seen.update(platforms)
