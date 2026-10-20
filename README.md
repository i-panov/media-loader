# media-loader

Кэширующий HTTP-сервис для скачивания медиа через `yt-dlp`, `ffmpeg`, Redis и byedpi.

Сервис принимает URL, один раз скачивает **полный source максимального качества** и хранит его в кэше. MP4, MKV, аудио и пониженное качество вычисляются из этого source по запросу. Перекодированные варианты в кэш не сохраняются.

## Возможности

- Один публичный медиа-эндпоинт `POST /videos`.
- Адаптивный Web UI `/ui` без npm и сборки.
- Синхронный режим `metadata_only=true`: только метаданные, без скачивания контента и без polling.
- Source-only кэш: один полный исходник на канонический URL.
- TTL и LRU-eviction для source-кэша.
- Форматы видео: MP4, MKV.
- Аудио: M4A, MP3 (по умолчанию), Opus, original.
- Качество: 240p–2160p с автоматическим clamp по качеству источника.
- Постоянное хранение метаданных в SQLite (JSON-колонка).
- Метаданные всегда возвращаются вместе с файлом в HTTP-заголовках.
- Bearer-токены, управляемые через CLI.
- byedpi sidecar для обхода DPI; проверенная конфигурация для YouTube.
- Redis AOF, отдельный worker, прогресс загрузки и перекодирования.
- Строгая статическая проверка и branch coverage с порогом 80%.

## Архитектура

```text
Клиент
  │  POST /videos
  ▼
API (FastAPI) ──────────────► Redis
  │                            │  очередь arq, статусы, токены,
  │  cache hit → файл           │  source-индекс, proxy-состояния
  │                            ▼
  │                         Worker (arq)
  │                            │
  │                            ├── yt-dlp ──► direct / byedpi ──► интернет
  │                            └── ffmpeg ──► remux / transcode
  │
  └── SQLite: media_metadata (JSON)
```

| Компонент | Назначение |
|---|---|
| `api` | FastAPI, авторизация, кэш-хиты, постановка задач, выдача файлов, статический UI |
| `worker` | arq, загрузка source, ffmpeg, прогресс, очистка кэша |
| `redis` | очередь, статусы, source-кэш, токены, состояния доменов; AOF |
| `byedpi` | SOCKS5 sidecar для обхода DPI, без публикации порта |
| SQLite | постоянные метаданные в `/data/metadata.sqlite3` |

## Быстрый старт

Требования: Docker с Compose v2.

```bash
cp .env.example .env
docker compose build
docker compose up -d
docker compose ps
```

Создать первый API-токен:

```bash
TOKEN=$(docker compose exec -T api media-loader-auth create --name phone --quiet)
echo "$TOKEN"
```

Без токена `POST /videos` отвечает `401`. `/health` доступен без авторизации.

## Web UI

Открыть:

```text
http://127.0.0.1:8000/ui
```

UI — статические HTML/CSS/JS, без npm и сборки. Отдельного frontend-контейнера нет: страницы раздаёт API-контейнер, файлы входят в wheel. Доступны:

- поле URL;
- режим «Видео / Только аудио»;
- видеоформат `MP4`/`MKV`;
- аудиоформат `MP3`/`M4A`/`Opus`/`original`;
- качество `240p`…`2160p` или «Максимум»;
- токен API (хранится в `localStorage`, не в URL);
- polling прогресса и кнопка скачивания.

После отправки URL страницы содержит параметры запроса:

```text
/ui?url=<encoded>&audio_only=false&format=mp4&quality=720p
```

Страницу можно перезагрузить — загрузка продолжится, если job ещё жив. Polling идёт повторением `POST /videos` с `respond_json=true`; одиночные сетевые сбои повторяются автоматически. Готовый файл отдаётся по короткоживущей ссылке `/downloads/<token>`; браузер стримит его на диск, не загружая целиком в память. Токен создаётся один на job и переиспользуется при повторных polling-запросах.

Статика UI публичная, но `/videos` требует Bearer-токен. Первый заход просит токен; его можно сменить кнопкой «Токен».

## Авторизация

Токен передаётся в заголовке:

```http
Authorization: Bearer <token>
```

Управление:

```bash
# создать именованный токен; plaintext показывается один раз
docker compose exec api media-loader-auth create --name phone

# список без plaintext
docker compose exec api media-loader-auth list

# отозвать
docker compose exec api media-loader-auth revoke --name phone
```

- Несколько именованных токенов.
- Срока действия нет; отзыв — вручную.
- В Redis хранится только `sha256(token)`, не сам токен.
- `last_used_at` обновляется с throttling.
- `AUTH_ENABLED=false` допустим только для локальной разработки.

## API

Публичный медиа-эндпоинт один: `POST /videos`. Polling выполняется повторением **того же запроса с тем же телом**.

### Запрос

```bash
curl -sS -X POST http://127.0.0.1:8000/videos \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.youtube.com/watch?v=...",
    "audio_only": false,
    "metadata_only": false,
    "format": "mp4",
    "quality": "720p"
  }' \
  -o /tmp/media-result \
  -w '%{http_code}\n'
```

| Поле | Тип | По умолчанию | Описание |
|---|---|---|---|
| `url` | string | — | HTTP/HTTPS URL |
| `audio_only` | bool | `false` | только аудио |
| `metadata_only` | bool | `false` | только метаданные, без контента |
| `respond_json` | bool | `false` | всегда отвечать JSON; для UI и получения download-ссылки |
| `format` | string | `mp4` / `mp3` | видео: `mp4`, `mkv`; аудио: `m4a`, `mp3`, `opus`, `original` |
| `quality` | string | максимум source | `240p`, `360p`, `480p`, `720p`, `1080p`, `1440p`, `2160p`; только для видео |

Для `audio_only=true` параметр `quality` запрещён (`422`).

### Ответы

| Код | Значение |
|---|---|
| `200` | Готовый файл (метаданные в заголовках), JSON при `metadata_only=true` или `respond_json=true` |
| `202` | Задача выполняется: статус, прогресс, время старта, `retry_after` |
| `401` | Нет, неверный или отозванный токен |
| `422` | Невалидный URL/формат/качество, live или playlist |
| `502` | Ошибка загрузки, yt-dlp или ffmpeg |
| `503` | Недоступен Redis/auth backend или не удалось поставить задачу |
| `507` | Не хватает свободного места |

### Polling

Если пришёл `202`, повторите тот же запрос. Когда обработка закончится, тот же `POST` вернёт `200` и файл.

```bash
while true; do
  code=$(curl -sS -X POST http://127.0.0.1:8000/videos \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"url":"https://www.youtube.com/watch?v=...","format":"mp4","quality":"720p"}' \
    -o /tmp/media-result \
    -w '%{http_code}')

  case "$code" in
    200) echo "ready: /tmp/media-result"; break ;;
    202) cat /tmp/media-result; sleep 5 ;;
    *)   cat /tmp/media-result; exit 1 ;;
  esac
done
```

Пример `202`:

```json
{
  "status": "processing",
  "stage": "transcoding",
  "source_key": "a1b2c3...",
  "source_cached": true,
  "media_type": "video",
  "format": "mp4",
  "requested_quality": "720p",
  "actual_quality": "720p",
  "source_quality": "1080p",
  "quality_clamped": false,
  "available_qualities": ["360p", "720p", "1080p"],
  "started_at": "2026-10-20T10:40:00+03:00",
  "started_at_local": "2026-10-20 10:40:00",
  "timezone": "Europe/Moscow",
  "progress": {
    "percent": 50.0,
    "stage": "transcoding",
    "downloaded_bytes": null,
    "total_bytes": null,
    "speed_bps": null,
    "eta_seconds": 12
  },
  "via_proxy": false,
  "attempts": 1,
  "retry_after": 5
}
```

Статусы: `queued`, `waiting_for_source`, `downloading_source`, `processing`.  
Стадии: `queued`, `downloading`, `waiting`, `merging`, `remuxing`, `transcoding`, `extracting`, `finalizing`.

Мгновенный `200` без job возможен, если source уже в кэше и запрошен ровно source-формат: `format=mkv`, качество не ниже исходного.

## Метаданные

### Только метаданные

```bash
curl -sS -X POST http://127.0.0.1:8000/videos \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.youtube.com/watch?v=...","metadata_only":true}' \
  | python3 -m json.tool
```

`metadata_only=true`:

- синхронный запрос, polling не нужен;
- контент не скачивается;
- job не создаётся;
- если source в кэше — ответ мгновенный;
- иначе выполняется лёгкий yt-dlp probe с тем же direct/byedpi fallback.

Возвращаются поля, которые отдаёт сервис/yt-dlp:

- `id`, `display_id`, `title`, `description`;
- `duration`, `duration_string`, `upload_date`, `timestamp`;
- `uploader`, `uploader_id`, `uploader_url`, `channel`, `channel_id`, `channel_url`;
- `view_count`, `like_count`, `comment_count`, `repost_count`;
- `tags`, `categories`, `genres`;
- `thumbnail`, `thumbnails`;
- `chapters`;
- `subtitles` и `automatic_captions` (языки);
- `formats` (сводка: format_id, ext, resolution, codecs, size, tbr);
- `available_qualities`, `width`, `height`, `fps`, `aspect_ratio`;
- `artist`, `album`, `track`, `release_year`, `creator`, `cast`, `director` и другие поля, если сервис их отдаёт;
- рассчитанные `media_type`, `format`, `requested_quality`, `actual_quality`, `source_quality`, `quality_clamped`, `source_cached`, `size`, `expires_at`.

### Метаданные вместе с файлом

При `200` с файлом метаданные всегда возвращаются в заголовках:

- `X-Media-Metadata` — base64url JSON со сводкой (description до 500 символов, tags до 10, categories до 5);
- `X-Media-Metadata-Encoding: base64url+json`;
- `X-Media-Title` — percent-encoded;
- `X-Media-Duration`;
- `X-Media-Upload-Date`;
- `X-Media-Uploader`;
- `X-Media-Source-Quality`;
- `X-Media-Actual-Quality`;
- `X-Media-Video-Codec`;
- `X-Media-Audio-Codec`;
- `X-Media-Size`;
- `X-Quality-Clamped`.

Полный набор метаданных доступен через `metadata_only=true`.

### SQLite

Метаданные сохраняются в `/data/metadata.sqlite3`:

```text
media_metadata
  source_key     TEXT PRIMARY KEY
  canonical_url  TEXT
  metadata_json  TEXT
  created_at     TEXT
  updated_at     TEXT
```

Запись выполняется:

- сразу после metadata-only probe;
- после успешной загрузки контента (с фактическими кодеками/качествами source).

Перед сетевым probe API сначала читает SQLite. Метаданные не имеют TTL; при загрузке контента строка обновляется. Это значит, что просмотры/лайки могут быть неактуальны, если контент давно не скачивался.

`GET /health` — без авторизации, проверяет Redis, SQLite и TCP-доступность byedpi.

### UI и download-ссылки

| Метод | Путь | Авторизация | Назначение |
|---|---|---|---|
| `GET` | `/ui` | нет | HTML-интерфейс |
| `GET` | `/static/*` | нет | CSS/JS/иконка |
| `GET` | `/ui/config` | нет | defaults, quality levels, форматы |
| `GET` | `/downloads/{token}` | нет | скачивание готового job-файла по capability-токену |

`respond_json=true` меняет ответ `200`: вместо файла возвращается JSON с `download_url`, `filename`, `content_type` и `size`. Download-токен создаётся один на job, переиспользуется при повторных запросах, живёт `JOB_TTL` (по умолчанию 1 час), одноразовым не является и удаляется вместе с job. Ссылка содержит секрет, поэтому её не стоит публиковать.

## Форматы и качество

### Видео (`audio_only=false`)

| Формат | Поведение |
|---|---|
| `mp4` | H.264/AAC; remux, если source совместим, иначе transcode |
| `mkv` | Без понижения — stream copy; при понижении — transcode видео в H.264 |

### Аудио (`audio_only=true`)

| Формат | Поведение |
|---|---|
| `m4a` | AAC copy или transcode в AAC |
| `mp3` | MP3 copy или transcode в MP3, по умолчанию |
| `opus` | Opus copy или transcode в Opus |
| `original` | лучший аудиопоток без перекодирования; контейнер по кодеку |

### Качество

- `quality` не указан → максимум source.
- Качество выше source не апскейлится: возвращается source, `quality_clamped=true`.
- Качество ниже source требует transcode.
- Для MKV при понижении качества видео кодируется в H.264, аудио копируется.
- Для MP4 целевые кодеки H.264/AAC.

## Как работает загрузка

1. URL канонизируется; вычисляется `source_key` и `request_key`.
2. API проверяет source-кэш и SQLite/Redis.
3. При необходимости создаётся job и ставится в arq.
4. Worker получает source:
   - direct TLS probe;
   - при неудаче — byedpi и пометка домена `proxy`;
   - direct fast-fail без долгих retry yt-dlp.
5. yt-dlp скачивает полный source максимального качества. `yt-dlp[default,deno]` устанавливает EJS и Deno прямо в venv (`/app/.venv/bin/deno`); JS-challenges YouTube решаются без отдельного runtime-образа.
6. Видео сохраняется как MKV (stream copy), аудио — в нативном контейнере.
7. Из source выполняется remux/transcode в запрошенный формат и качество.
8. Готовый производный файл живёт в `/data/jobs/<request_key>/` до `JOB_TTL`.
9. Source остаётся в кэше до TTL/eviction.

Даже `audio_only=true` и низкое качество сначала требуют полного source максимального качества — это следствие source-only кэша.

## Кэш

- Ключ: `sha256(канонический URL)`, без формата и качества.
- YouTube-варианты (`youtu.be`, `shorts`, `embed`, `live`, `watch?v=`) приводятся к одному URL.
- В кэше один MKV (лучшие video+audio) или нативный аудиофайл для аудио-платформ.
- TTL фиксированный от завершения загрузки, по умолчанию 24 часа.
- Лимит размера по умолчанию 5 GB; при переполнении удаляются least recently used source.
- Source, который скачивается или используется деривацией, не вытесняется.
- Производные файлы не кэшируются и в лимит source-кэша не входят.
- Перед загрузкой проверяется свободное место; при нехватке — `507`.
- Очистка запускается cron’ом worker’а и после успешных загрузок. `CACHE_CLEANUP_INTERVAL` определяет шаг cron’а: до часа — минутная сетка, от часа — часовая; интервалы округляются.

## byedpi и YouTube

byedpi собирается из исходников `hufrea/byedpi` v0.17.3 в отдельный sidecar и слушает `byedpi:1080`. Наружу порт не публикуется.

Аргументы задаются в `.env`:

```dotenv
BYEDPI_ARGS=--auto=torst --timeout 3 --tlsrec 3+s
```

Эта конфигурация проверена на полной загрузке YouTube: source MKV, MP3 и MP4 успешно получены.

### Как выбирается direct/proxy

1. Для неизвестного домена выполняется быстрый TLS probe (`DIRECT_PROBE_TIMEOUT=5`). Это осознанный trade-off: сайт с невалидным сертификатом или MITM-прокси провайдера может быть помечен `proxy`, даже если HTTP direct формально работает.
2. Если probe не проходит, домен сразу помечается `proxy`, и запрос идёт через byedpi.
3. Если direct-запрос падает с транспортной ошибкой, домен помечается `proxy` на `PROXY_STATE_TTL=24h`, запрос повторяется через byedpi.
4. Direct-попытка использует fast-fail без длинных внутренних retry yt-dlp.

Первый запрос к заблокированному домену может быть медленнее: TLS probe + соединение через byedpi.

### Если YouTube перестал открываться

Стратегии зависят от провайдера, региона и времени. Порядок действий:

```bash
# 1. Проверить текущую стратегию
docker compose exec worker yt-dlp --proxy socks5://byedpi:1080 --simulate \
  --print '%(title)s' 'https://www.youtube.com/watch?v=jNQXAC9IVRw'

# 2. Изменить BYEDPI_ARGS в .env и пересоздать sidecar
docker compose up -d --force-recreate byedpi

# 3. Сбросить запомненное состояние домена
docker compose exec api media-loader-admin proxy-reset youtube.com
```

Варианты стратегий:

- `--tlsrec 1+s`;
- `--fake -1 --ttl 8 --auto=ssl_err --fake -1 --ttl 5 --udp-fake 1`;
- `--fake -1 --ttl 8 --udp-fake 1`;
- автоподбор через `xmaxzzz/byedpi-finder` или аналог.

## Настройки

Все параметры — в `.env.example`. Длительности и размеры можно задавать как `24h`, `30m`, `5GB`, `512MiB`.

### Сервер и хранилище

| Переменная | По умолчанию | Описание |
|---|---|---|
| `SERVER_TIMEZONE` | `Europe/Moscow` | отображение времени |
| `CACHE_TTL` | `24h` | срок жизни source |
| `CACHE_MAX_SIZE` | `5GB` | лимит source-кэша |
| `CACHE_CLEANUP_INTERVAL` | `300` | шаг cron очистки, сек; до часа — минуты, от часа — часы, >24h — раз в сутки |
| `METADATA_DB_PATH` | `/data/metadata.sqlite3` | SQLite с метаданными |
| `MIN_FREE_DISK` | `1GB` | минимальный свободный запас |
| `JOB_TTL` | `1h` | срок жизни производных файлов |
| `JOB_TIMEOUT` | `12h` | таймаут задачи |
| `SOURCE_LOCK_TTL` | `120` | lease source-lock, сек; продлевается heartbeat |
| `QUEUED_REENQUEUE_SECONDS` | `10` | self-heal зависшего queued job |
| `MAX_CONCURRENT_JOBS` | `2` | одновременные job’ы worker’а |
| `MAX_CONCURRENT_TRANSCODES` | `1` | одновременные ffmpeg transcode |
| `REDIS_URL` | `redis://redis:6379/0` | Redis |
| `RETRY_AFTER_SECONDS` | `5` | `Retry-After` для 202 |
| `FAILED_RETRY_SECONDS` | `60` | cooldown перед повтором failed job |
| `FAILED_RECORD_TTL` | `300` | сколько хранить failed job |

### Медиа

| Переменная | По умолчанию | Описание |
|---|---|---|
| `DEFAULT_VIDEO_FORMAT` | `mp4` | формат видео по умолчанию |
| `DEFAULT_AUDIO_FORMAT` | `mp3` | формат аудио по умолчанию |
| `QUALITY_LEVELS` | `240p,...,2160p` | допустимые качества |
| `VIDEO_CRF` | `18` | CRF для libx264 |
| `VIDEO_PRESET` | `medium` | preset libx264 |
| `AUDIO_AAC_BITRATE` | `256k` | AAC transcode |
| `AUDIO_MP3_BITRATE` | `320k` | MP3 transcode |
| `AUDIO_OPUS_BITRATE` | `160k` | Opus transcode |
| `YTDLP_COOKIES_FILE` | `/config/cookies.txt` | cookies для yt-dlp |

### byedpi

| Переменная | По умолчанию | Описание |
|---|---|---|
| `BYEDPI_ARGS` | `--auto=torst --timeout 3 --tlsrec 3+s` | аргументы ciadpi |
| `PROXY_URL` | `socks5://byedpi:1080` | SOCKS5 |
| `PROXY_STATE_TTL` | `24h` | сколько помнить режим домена |
| `DIRECT_SOCKET_TIMEOUT` | `15` | socket timeout yt-dlp |
| `DIRECT_PROBE_TIMEOUT` | `5` | TLS probe домена |

### Авторизация

| Переменная | По умолчанию | Описание |
|---|---|---|
| `AUTH_ENABLED` | `true` | включить Bearer-токены |
| `AUTH_TOKEN_PREFIX` | `mlk_` | префикс токена |
| `AUTH_LAST_USED_UPDATE_INTERVAL` | `60` | throttling `last_used_at` |
| `DOCS_ENABLED` | `false` | `/docs` и `/openapi.json` |
| `API_PORT` | `8000` | host-порт Compose |

Cookies для yt-dlp можно положить в `./config/cookies.txt`. Файл не попадает в образ и игнорируется git.

`DOCS_ENABLED=true` включает документацию, но она защищена тем же auth-middleware. Для просмотра в браузере на localhost можно временно поставить `AUTH_ENABLED=false`.

## CLI

```bash
# токены
docker compose exec api media-loader-auth create --name phone
docker compose exec api media-loader-auth create --name phone --quiet
docker compose exec api media-loader-auth list
docker compose exec api media-loader-auth revoke --name phone

# кэш, SQLite и proxy-состояния
docker compose exec api media-loader-admin cache-stats
docker compose exec api media-loader-admin proxy-reset youtube.com

# healthcheck зависимостей
docker compose exec api media-loader-healthcheck
```

`cache-stats` показывает размер source-кэша, размер job/tmp, число строк и размер SQLite.

## Данные на диске

```text
data/
  cache/<source_key>/source.mkv       # source максимального качества
  cache/<source_key>/source.m4a       # для аудио-платформ
  jobs/<request_key>/...              # временный производный файл
  tmp/                                # временные загрузки
  metadata.sqlite3                    # JSON-метаданные

config/
  cookies.txt                         # опционально, не в git
```

Redis хранится в named volume `media-loader_redis_data`.

## Production deployment

Текущая production-схема — один хост с Docker Compose. Все runtime-зависимости находятся в образах; на хосте нужны только Docker и Compose, без Python, ffmpeg, Deno и yt-dlp.

### Что запускается

| Контейнер | Процесс |
|---|---|
| `api` | `uvicorn media_loader.api:app --host 0.0.0.0 --port 8000` |
| `worker` | `arq media_loader.worker.WorkerSettings` |
| `redis` | `redis-server` с AOF и volume |
| `byedpi` | `ciadpi` с SOCKS5 на `byedpi:1080` |

`uvicorn` — ASGI-сервер приложения. Отдельный gunicorn/nginx внутри контейнера не используется. В образе API/worker уже есть Python, uv-venv, yt-dlp, Deno, ffmpeg и ffprobe; Redis и byedpi — отдельные образы Compose.

API по умолчанию опубликован только на `127.0.0.1:8000` и не доступен из интернета напрямую.

### Первый деплой

```bash
git clone git@github.com:i-panov/media-loader.git
cd media-loader
cp .env.example .env
# отредактировать .env: BYEDPI_ARGS, SERVER_TIMEZONE, AUTH_ENABLED и т.д.

docker compose build
docker compose up -d
docker compose ps

docker compose exec -T api media-loader-auth create --name prod --quiet
```

Проверка:

```bash
curl -sS http://127.0.0.1:8000/health
```

### Обновление

```bash
git pull
docker compose build
docker compose up -d
docker compose ps
```

Схема SQLite создаётся автоматически. Отдельной системы миграций пока нет: при изменении схемы БД понадобится ручная миграция.

### TLS и reverse proxy

Bearer-токены нельзя передавать по открытому HTTP. Наружу открываются только 80/443; порт API остаётся на loopback.

Пример Caddy на хосте:

```caddyfile
media.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
```

Если Caddy запускается контейнером в той же Compose-сети, upstream — `api:8000`, а публикацию порта API можно убрать. Firewall должен разрешать только 22/80/443; `8000` наружу не открывать.

UI доступен по `/ui` через тот же proxy. Статика UI публичная, но запросы к `/videos` требуют токен; download-ссылки `/downloads/<token>` короткоживущие и являются capability URL.

### Логи

Docker-логи по умолчанию могут расти без ограничений. Для production задайте ротацию, например для `api` и `worker`:

```yaml
logging:
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"
```

### Бэкапы

- `data/metadata.sqlite3` — метаданные. Используйте SQLite backup API или `VACUUM INTO`, а не обычный `cp` при работающем WAL:
  ```bash
  docker compose exec -T api python -c "
  import sqlite3
  src = sqlite3.connect('/data/metadata.sqlite3')
  dst = sqlite3.connect('/data/metadata.backup.sqlite3')
  src.backup(dst)
  dst.close()
  src.close()
  "
  docker compose cp api:/data/metadata.backup.sqlite3 ./metadata.backup.sqlite3
  ```
- Redis volume — токены, job-статусы, proxy-состояния. При потере Redis токены нужно создать заново.
- `config/cookies.txt` — если используется.
- `data/cache` можно не бэкапить: source пересоздаётся из исходных URL, если он ещё доступен.

### Масштабирование и ограничения

- API можно масштабировать несколькими репликами за load balancer’ом: он использует Redis/SQLite, но не хранит состояние job в памяти.
- `worker` сейчас рассчитан на один экземпляр: startup-recovery и source-lock lease не являются распределённым координатором. Не используйте `docker compose up --scale worker=2` без доработки.
- SQLite подходит для одного хоста. Для нескольких хостов и HA нужны Postgres/Redis и объектное хранилище.
- Метрик и per-token rate limit пока нет; для внешнего прода добавьте мониторинг и ограничения на reverse proxy/API.
- Для горизонтального масштабирования media-файлов понадобится отдельное объектное хранилище; локальный `data/cache` — single-host решение.

### Production checklist

- [ ] Домен и TLS настроены, API не открыт напрямую.
- [ ] Firewall: только SSH и 80/443.
- [ ] Создан и сохранён production-токен; старые тестовые токены отозваны.
- [ ] Настроены ротация логов и мониторинг диска.
- [ ] Настроен бэкап SQLite, Redis volume и cookies.
- [ ] `worker` запущен в одном экземпляре.
- [ ] Проверены `/health`, metadata-only и реальная загрузка через byedpi.

## Разработка

Требования: Python 3.13, uv, ffmpeg/ffprobe для ffmpeg-тестов.

```bash
uv sync --locked

uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pyright src

uv run deno fmt --check src/media_loader/static/
uv run deno lint src/media_loader/static/app.js
uv run deno check src/media_loader/static/app.js

uv run pytest
uv run pytest --cov=media_loader --cov-report=term-missing
```

Сборка и запуск контейнеров:

```bash
docker compose config
docker compose build
docker compose up -d
```

## Качество кода

- Ruff: `E/F/I/UP/B/SIM/RUF/ANN/ARG/ASYNC/C4/DTZ/PTH/RET/PERF/LOG/G/N/A/PT`, line length 100.
- Mypy `strict` для `src` и `tests`.
- Pyright `strict` для `src`; `reportUnknown*` отключены для динамических словарей yt-dlp/Redis, `ANN401` в Ruff — для явного `Any` на внешних границах.
- UI JS проверяется установленным Deno: `deno fmt --check`, `deno lint`, `deno check`.
- Pytest: 111 тестов, branch coverage с порогом **80%**; текущее покрытие ~86%.
- ffmpeg-тесты автоматически пропускаются, если ffmpeg/ffprobe не установлены.
- CI `.github/workflows/ci.yml`: ruff, mypy, pyright, Deno-проверки UI, тесты с покрытием и `docker compose build`.

## Обновление зависимостей

yt-dlp установлен как nightly и зафиксирован в `uv.lock`:

```bash
uv lock --upgrade-package yt-dlp --prerelease allow
uv sync --locked
docker compose build api worker
docker compose up -d --force-recreate api worker
```

Версия byedpi зафиксирована в `docker/byedpi.Dockerfile`; после изменения версии/коммита:

```bash
docker compose build byedpi
docker compose up -d --force-recreate byedpi
```

## Troubleshooting

| Симптом | Что проверить |
|---|---|
| `401` | токен отсутствует/отозван; `media-loader-auth list` |
| `422` | URL, `format`, `quality`, live или playlist |
| `502` | `docker compose logs worker`; ошибка yt-dlp/ffmpeg |
| `503` | Redis, auth backend или постановка задачи |
| `507` | свободное место и `MIN_FREE_DISK` |
| YouTube не открывается | `BYEDPI_ARGS`, `proxy-reset youtube.com`, проверка через `yt-dlp --proxy` |
| Первый запрос медленный | TLS probe + byedpi; дальше домен в состоянии `proxy` |
| Metadata-only медленный первый раз | выполняется сетевой probe; дальше ответ из SQLite |
| Аудио/низкое качество качается долго | сначала скачивается полный source |
| Transcode долгий | CPU, 4K/HDR, `MAX_CONCURRENT_TRANSCODES`, preset/CRF |
| `/docs` даёт `401` | docs защищены auth; для локального просмотра `DOCS_ENABLED=true` и `AUTH_ENABLED=false` |
| Долгий `202` после падения worker | lease истекает через `SOURCE_LOCK_TTL`, затем job перехватывается |
| `worker_restarted` | worker был перезапущен; повторите запрос после cooldown |

## Ограничения

- Live-стримы и плейлисты не поддерживаются.
- DRM не поддерживается.
- Целевой WebM/AV1 не входит в MVP.
- yt-dlp делает pre-probe и затем download-extract; для YouTube это два прохода по страницам. Оставлено ради ранней валидации playlist/live и выбора форматов.
- Startup-recovery рассчитан на один worker.
- Нет ролей/пользователей: только именованные Bearer-токены.
- Нет per-token rate limit; защита от спама — только выдача/отзыв токенов.
- API слушает loopback. Для внешнего доступа нужен HTTPS/reverse proxy.
- Низкое качество и аудиоформаты требуют перекодирования из max-source.
- HDR/10-bit при transcode в H.264 8-bit теряют HDR.
- Метаданные в SQLite не имеют TTL и могут устаревать; обновляются при загрузке контента.
- SSE не реализован; в MVP используется polling.

## Структура проекта

```text
src/media_loader/
  api.py           # FastAPI, единый POST /videos, auth middleware
  worker.py        # arq: source download, ffmpeg, cleanup
  ytdlp.py         # yt-dlp probe/download, метаданные
  transcode.py     # ffprobe, remux/transcode, ffmpeg progress
  netcheck.py      # TLS probe и direct/byedpi fallback
  store.py         # Redis: source-кэш, job, TTL/LRU, locks
  metadata_db.py   # SQLite JSON-хранилище метаданных
  canonical.py     # канонизация URL и ключи
  quality.py       # quality ladder, clamp, scale
  progress.py      # прогресс загрузки и ffmpeg
  auth.py          # токены и CLI
  admin.py         # cache-stats, proxy-reset
  healthcheck.py   # Docker healthcheck
  schemas.py       # запросы и форматы
  config.py        # настройки
  errors.py        # типизированные ошибки
  utils.py         # файлы, время, content types
  static/          # index.html, styles.css, app.js, favicon.svg

tests/             # unit/API/ffmpeg/CLI/UI тесты
docker/            # byedpi Dockerfile и entrypoint
```
