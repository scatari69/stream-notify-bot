# 🔴 Stream Notifier — Discord Live Bot

A production-ready Discord bot that notifies your server the moment you go
**live** on **YouTube** or **Twitch** — with a clean, extensible
architecture that makes adding new platforms trivial.

Built with **Python 3.12+** and **discord.py 2.x**, fully async, type-hinted,
logged and configurable.

---

## ✨ Features

- **Multi-platform live detection** — YouTube and Twitch out of the box.
- **Pluggable architecture** — add a new platform by dropping one file in
  `platforms/` and registering it. Every platform implements `is_live()` and
  `get_stream_info()`.
- **Duplicate protection** — persists sent notifications and checks channel history before retrying uncertain sends.
- **Offline detection** — resets internal state when a stream ends, ready for
  the next one.
- **Beautiful embeds** — title, platform, thumbnail, start time, channel name,
  viewer count, category and stream URL, with platform-specific emojis and
  colour themes.
- **Configurable buttons** — ▶️ Watch Stream, 💬 Join Discord, 📺 Subscribe.
- **Role mention** — optionally pings a "Live" role (once per stream).
- **Dynamic presence** — `Watching for live streams...` → `Streaming <name> LIVE`.
- **Admin slash commands** — `/createconfig`, `/status`, `/forcecheck`,
  `/testnotification`, `/platforms`, `/help`.
- **Automatic retry** with exponential backoff when APIs fail.
- **Statistics** — notifications sent, per-platform counts, uptime, checks run.
- **Bonus options** — auto-update thumbnail/viewers, auto-delete the
  notification when the stream ends, multi-server delivery and custom themes.

---

## 📁 Project structure

```
stream-notify-bot/
├── main.py               # Entrypoint: load config, connect, start monitoring
├── monitor.py            # Polling loop, dedup, offline detection, presence
├── config.example.json   # Copy to config.json and fill in (non-secret values)
├── .env.example          # Copy to .env and fill in (secrets)
├── requirements.txt
├── README.md
├── platforms/
│   ├── base.py           # BasePlatform ABC + StreamInfo dataclass + retry
│   ├── youtube.py        # YouTube (Data API v3 + HTML fallback)
│   ├── twitch.py         # Twitch (Helix API + OAuth client credentials)
│   └── __init__.py       # Platform registry / factory
├── utils/
│   ├── config.py         # Typed config loading + validation
│   ├── embeds.py         # Embed + button builders
│   └── logger.py         # Timestamped, coloured logging (+ LIVE/OFFLINE levels)
└── commands/
    └── admin.py          # Admin-only slash commands
```

---

## 🚀 Setup

### 1. Requirements

- Python **3.12+**

```bash
git clone <your-repo-url>
cd stream-notify-bot
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Create the Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. **New Application** → **Bot** → copy the **Token**.
3. Invite the bot to your server with the `bot` and `applications.commands`
   scopes and permission to **Send Messages** / **Embed Links** (and **Mention
   Everyone** if you use a live role).

### 3. Configure secrets (`.env`)

```bash
cp .env.example .env
```

Fill in:

| Variable | Required? | Notes |
| --- | --- | --- |
| `DISCORD_TOKEN` | ✅ | Your bot token. |
| `YOUTUBE_API_KEY` | ⚪ optional | [YouTube Data API v3](https://console.cloud.google.com/apis/library/youtube.googleapis.com) key. Without it, YouTube falls back to HTML scraping. |
| `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` | ⚪ optional | Needed only if you monitor Twitch. Create an app at [dev.twitch.tv](https://dev.twitch.tv/console/apps). |

### 4. Configure the bot (`config.json`)

```bash
cp config.example.json config.json
```

Each entry in `servers` contains its own Discord destination and `streams` list.
Replace the example IDs and channel names before starting:

```json
"servers": [
  {"guild_id": 111, "notification_channel_id": 222, "live_role_id": 0,
   "streams": [{"name": "Streamer", "youtube_channel_id": "UC...", "twitch_username": ""}]}
]
```

Add more servers or streams to the respective arrays. `poll_interval` is in seconds
(minimum 10); `custom_message` supports `{name}`, `{streamer}`, `{platform}` and `{url}`.
Buttons, themes and other presentation options remain global.

### 5. Run

```bash
python main.py
```

You should see:

```
2026-06-29 01:34:00 [INFO] stream-notify: Configuration loaded and validated successfully.
2026-06-29 01:34:01 [INFO] stream-notify: Connected as YourBot#1234 (id: ...)
2026-06-29 01:34:01 [INFO] stream-notify.monitor: Checking YouTube...
2026-06-29 01:34:02 [LIVE] stream-notify.monitor: 📺 Alpha went live: Grinding Ranked!
```

---

## 🧩 Adding a new platform

1. Create `platforms/myplatform.py`:

   ```python
   from platforms.base import BasePlatform, StreamInfo

   class MyPlatform(BasePlatform):
       name = "myplatform"
       display_name = "MyPlatform"
       emoji = "🎬"

       @property
       def configured(self) -> bool:
           return bool(self.config.raw.get("myplatform_username"))

       async def get_stream_info(self) -> StreamInfo | None:
           ...  # return StreamInfo if live, else None
   ```

2. Register it in `platforms/__init__.py`:

   ```python
   from platforms.myplatform import MyPlatform
   PLATFORM_REGISTRY = (YouTubePlatform, TwitchPlatform, MyPlatform)
   ```

Done — the monitor will start polling it automatically.

---

## 🛠️ Slash commands (admin only)

| Command | Description |
| --- | --- |
| `/createconfig` | Download a fresh `config.json` template. |
| `/status` | Uptime, stats, poll interval and per-platform live status. |
| `/forcecheck` | Run an immediate check across all platforms. |
| `/testnotification` | Post a sample notification to verify formatting. |
| `/platforms` | List platforms and per-platform notification counts. |
| `/help` | Show command help. |

---

## ❓ Troubleshooting

- **`Configuration error: Discord token missing`** — set `DISCORD_TOKEN` in
  `.env`.
- **YouTube never detects live** — add a `YOUTUBE_API_KEY`; the scraping
  fallback can be blocked by consent pages in some regions.
- **Twitch error about client id/secret** — set `TWITCH_CLIENT_ID` and
  `TWITCH_CLIENT_SECRET`.

---

## 📜 License

MIT — do whatever you like, no warranty.

При ошибке проверки Twitch или YouTube бот сохраняет последнее известное состояние и повторяет проверку позже. Подтверждённые уведомления и незавершённые отправки хранятся в `data/deliveries.sqlite3`: сохраните каталог `data` между перезапусками и запускайте только один экземпляр бота. При потере ответа Discord бот ищет своё сообщение в истории канала перед повтором; нужны права просмотра канала и чтения истории. Если историю прочитать нельзя, повтор откладывается. Защита не абсолютна: удаление сообщения или базы, рассинхронизация часов либо задержка появления сообщения в истории могут привести к повтору. Если включено удаление после эфира, бот проверит завершившиеся во время простоя трансляции и удалит уведомления после запуска; при ошибке проверки или удаления повторит попытку. При использовании YouTube без API-ключа изменённая или заблокированная HTML-страница может оставить состояние неопределённым.

## Локальная веб-админка

1. Установите зависимости проекта и создайте `.env` из `.env.example`.
2. В Discord Developer Portal укажите OAuth2 Redirect URL `http://127.0.0.1:8765/callback`. Заполните в `.env` параметры `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` и `DISCORD_OWNER_ID` (ID вашей учётной записи Discord).
3. Запустите `.venv/bin/python admin_web.py` и откройте http://127.0.0.1:8765 на этом компьютере. Войдите через Discord. Владелец может задать токен бота и API-ключи в разделе общих токенов; затем добавьте бота на нужные серверы.
4. После входа выберите сервер. В обзоре показаны число стримеров, время последней проверки, состояние каждой площадки и ошибки проверки, доставки и удаления; старые данные помечаются как неактуальные.
5. В настройках выберите канал и роль, добавьте стримеров, выберите текстовое сообщение или embed, задайте шаблон и при необходимости включите удаление после эфира. `{name}`, `{streamer}`, `{platform}`, `{url}` подставляются в текст; в простое сообщение ссылка добавляется автоматически. Участника Discord можно связать со стримером в выпадающем списке: `{streamer}` упомянет его; без привязки подставится имя стримера. Кнопка «Пауза» сохраняет запрет уведомлений для выбранного стримера, продолжая проверять его статус; после возобновления бот может сообщить об уже идущем эфире, если ещё не отправлял уведомление. Для списка включите **Server Members Intent** в разделе Bot в Discord Developer Portal (для проверенных приложений может потребоваться разрешение Discord). При сохранении бот проверяет права администратора и принадлежность канала, роли и участника выбранному серверу.

Бот (`.venv/bin/python main.py`) автоматически перечитывает корректные настройки и API-ключи через несколько секунд; изменение токена Discord вызывает переподключение с сохранением состояния стримов. Новый токен должен принадлежать тому же боту. Неправильная конфигурация не заменяет текущую. Секреты панели хранятся локально в `.secrets.json` с правами владельца; OAuth client secret и ID владельца задаются в `.env`. Для локального запуска панель слушает только localhost; запуск через HTTPS-прокси описан ниже. Сессии хранятся в памяти и сбрасываются при перезапуске панели.

Проверка: `.venv/bin/python -B -m unittest discover`.

## Запуск через Docker Compose

1. В каталоге проекта создайте файл настроек и заполните его:

   ```bash
   cp .env.example .env
   ```

   Укажите `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_OWNER_ID` и `PUBLIC_BASE_URL` — внешний HTTPS-адрес без завершающего слэша и пути, например `https://streams.example.com`. Токен бота можно указать в `DISCORD_TOKEN` или позже сохранить через панель. Если используете Twitch, добавьте его ключи через панель или `.env`.

2. В Discord Developer Portal добавьте в OAuth2 Redirects точный адрес `https://streams.example.com/callback` с вашим доменом. Пригласите бота на сервер. Для выпадающего списка участников включите Server Members Intent.
3. Настройте существующий реверс-прокси: завершение TLS для выбранного домена и проксирование на `http://127.0.0.1:8765` на машине Docker. Прокси должен сохранять исходный заголовок `Host`, включая порт, если он указан в `PUBLIC_BASE_URL`. Админка рассчитана на отдельный домен или поддомен, без префикса пути. Проверка `Origin` и OAuth используют только заданный `PUBLIC_BASE_URL`, а не доверяют заголовкам `X-Forwarded-*`.
4. Запустите панель, войдите через Discord, при необходимости сохраните токен бота и настройте хотя бы один сервер:

   ```bash
   docker compose up -d --build admin
   ```

5. Запустите бота:

   ```bash
   docker compose up -d bot
   ```

Настройки, секреты, состояние доставки и статус хранятся в именованном томе `stream_notify_data` и сохраняются при пересоздании контейнеров. Не удаляйте этот том при обновлении: удаление сотрёт эти данные. Рабочий каталог внутри тома доступен обоим сервисам.

```bash
docker compose ps
docker compose logs -f admin bot
docker compose up -d --build
docker compose stop
```

По умолчанию порт админки публикуется только на `127.0.0.1:8765` хоста. Если прокси работает в отдельном контейнере, его `127.0.0.1` означает сам контейнер: подключите админку и прокси к общей Docker-сети и используйте адрес `http://admin:8765` внутри этой сети либо настройте доступ к опубликованному порту хоста. Подключение прокси к сети нужно сохранить в его конфигурации Compose, иначе оно пропадёт при пересоздании контейнера. Интерфейс и порт публикации задаются через `ADMIN_BIND_ADDRESS` и `ADMIN_PORT`; при изменении интерфейса обеспечьте доступ к порту только со стороны прокси. Внутренний порт приложения — `8765`. Не публикуйте HTTP-порт напрямую в интернет.

Для локального запуска без прокси задайте `PUBLIC_BASE_URL=http://127.0.0.1:8765` и зарегистрируйте `http://127.0.0.1:8765/callback` в Discord. При изменении `.env` пересоздайте сервисы командой `docker compose up -d --force-recreate`.
