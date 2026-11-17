# AGENTS.md

Практическое руководство для AI-агентов (и людей), которые меняют код в этом репозитории.
Источник истины — код и CI (`.github/workflows/ci.yml`); [README.md](README.md) — пользовательская
документация, она может отставать от кода.

## Жёсткое правило: read-only

При файловой политике `read-only` запрещено изменять, создавать или удалять любые файлы,
а также запрашивать escalation и любым способом обходить ограничение.
Разрешены только чтение и команды, не меняющие состояние.
Если для задачи нужна запись — остановиться и сообщить пользователю.

## Что это за проект

`media-loader` — кэширующий HTTP-сервис скачивания медиа. Принимает URL, **один раз** скачивает
полный source максимального качества через `yt-dlp`, хранит его в кэше и по запросу вычисляет
MP4/MKV, аудио и пониженное качество через `ffmpeg`. Деривативы не кэшируются.
Основной режим — одно видео (`scope=episode`, включая одиночные ролики вне сериалов).
Дополнительно умеет скачивать сезон/все сезоны сериала (`scope=season|series`): это коллекция
отдельных файлов с номерами `S01E02` и метаданными сезона/серии. Для сезона можно выбрать
подмножество серий (`episodes`), а `scope=auto` определяет сериал по ссылке/заголовку.

- Python 3.13, пакет в `src/media_loader` (src-layout), сборка `uv_build`.
- API: FastAPI + uvicorn. Очередь: arq + Redis. Метаданные: SQLite. Медиа: ffmpeg/ffprobe.
- Обход DPI: byedpi (SOCKS5 sidecar) + пул стратегий.
- UI: статические HTML/CSS/JS без npm и сборки.
- Деплой: один хост, Docker Compose (сервисы `api`, `worker`, `redis`, `byedpi`).

## Быстрый старт

```bash
uv sync --locked                    # зависимости + dev-группа (ruff, mypy, pyright, pytest, deno)
uv run pytest -q                    # тесты; сеть, Redis и Docker не нужны
uv run ruff format .                # применить форматирование
uv run ruff check .                 # линт
uv run mypy src tests               # strict mypy
uv run pyright src                  # strict pyright (только src)
```

Docker для тестов не требуется: используются `fakeredis`, `httpx.ASGITransport` и monkeypatch
вместо реальной сети. `ffmpeg`/`ffprobe` нужны только для интеграционных тестов — без них они
автоматически пропускаются.

## Проверки (паритет с CI)

Перед завершением задачи прогоните то же, что CI:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pyright src
uv run deno fmt --check src/media_loader/static/
uv run deno lint src/media_loader/static/app.js
uv run deno check src/media_loader/static/app.js
uv run pytest --cov=media_loader --cov-report=term-missing
```

`deno` доступен через `uv run` (ставится зависимостью `yt-dlp[deno]`). Порог покрытия —
`fail_under = 80` (branch coverage) в [pyproject.toml](pyproject.toml); CI дополнительно собирает
образы (`docker compose build`).

Один тест:

```bash
uv run pytest tests/test_api.py -k cache_hit -q
```

## Архитектура

```text
Клиент ── POST /videos ──► API (FastAPI) ──► Redis (очередь arq, статусы, кэш, токены)
                               │                  │
                               │ cache hit ──► файл
                               ▼                  ▼
                          SQLite            Worker (arq)
                          metadata            ├── yt-dlp ──► direct / byedpi ──► интернет
                                              └── ffmpeg ──► remux/transcode
```

| Компонент | Роль |
|---|---|
| `api` | Авторизация, кэш-хиты, постановка job'ов, выдача файлов, `/health`, статика UI |
| `worker` | arq: скачивание source, ffmpeg, прогресс, коллекции сезонов, очистка кэша, recovery |
| `redis` | Очередь, статусы, source-кэш, коллекции, токены, состояния доменов, стратегии прокси, локи; AOF |
| `byedpi` | Пул `ciadpi` на портах `1080..1080+N`, SOCKS5, наружу не публикуется |
| SQLite | `/data/metadata.sqlite3`, JSON-метаданные без TTL |

Поток `POST /videos`:

1. Bearer-токен проверяется middleware ([api.py](src/media_loader/api.py)).
2. URL канонизируется, вычисляются `source_key` и `request_key` ([canonical.py](src/media_loader/canonical.py)).
3. Ответ зависит от состояния:
   - `metadata_only=true` — синхронный JSON, без job'а; сначала source-кэш, затем SQLite, затем сетевой probe;
   - source в кэше, запрошен MKV без понижения качества и `respond_json=false` — мгновенный `200`
     с файлом (`X-Cache: HIT`);
   - готовый job — файл либо JSON с `download_url` (при `respond_json=true`);
   - для `scope=season|series` — коллекция: список серий, отдельные job'ы и JSON-манифест;
   - иначе job создаётся в Redis (`SET NX`) и ставится в arq — `202` с прогрессом и `Retry-After`.
4. Polling — повторение **того же** `POST` с тем же телом.
5. Worker берёт source-lock, качает source (direct/proxy), приводит видео к MKV, снимает метаданные
   через ffprobe, строит дериватив (hardlink/copy или ffmpeg) в `/data/jobs/<request_key>/`.
6. Готовый файл живёт `JOB_TTL`, скачивается по capability-ссылке `/downloads/<token>`.

## Данные и ключи

Диск (внутри контейнера, хост-каталог задаётся `HOST_DATA_DIR`):

```text
/data/cache/<source_key>/source.mkv        # source максимального качества
/data/cache/<source_key>/source.<ext>      # аудио-платформы
/data/jobs/<request_key>/<filename>        # дериватив; JOB_TTL, для коллекций COLLECTION_TTL
/data/tmp/                                 # временные загрузки
/data/metadata.sqlite3                     # JSON-метаданные (WAL)
```

Redis (все ключи с префиксом `ml:`):

| Префикс | Назначение |
|---|---|
| `ml:source:` + zset'ы `ml:source:ttl`, `ml:source:lru`, `ml:source:size` | source-записи, TTL/LRU-учёт размера |
| `ml:source:active:` | счётчик активных использований source (защита от вытеснения) |
| `ml:source:progress:`, `ml:job:progress:` | хэши прогресса |
| `ml:job:` | записи job'ов |
| `ml:collection:` | записи коллекций (сезон/сериал), статусы серий |
| `ml:series:` | кэш разрешённого списка сезонов/серий |
| `ml:download:` / `ml:download-job:` | download-токен ↔ job |
| `ml:lock:` | локи (source, probe) |
| `ml:auth:names`, `ml:auth:token:` | имена и SHA-256-хэши токенов |
| `ml:domain:` | запомненный режим домена `direct`/`proxy` |
| `ml:proxy:strategy:`, `ml:proxy:probe:` | выбранная стратегия byedpi и лок перебора |

## Инварианты, которые нельзя ломать

1. **Source-only кэш.** Один полный source максимального качества на канонический URL. Не кэшируйте
   деривативы и не «оптимизируйте» аудио/низкое качество отдельными загрузками.
2. **Ключи.** `source_key = sha256(canonical_url)[:32]`,
   `request_key = sha256(url|type:format|quality_or_"max")[:32]`.
   Смена канонизации или формулы ключей — breaking change: инвалидирует кэш и висящие download-ссылки.
3. **Файловые операции.** Всё в пределах `/data` на одной ФС. Перемещения — только через
   `atomic_move`, копии — через `hardlink_or_copy` ([utils.py](src/media_loader/utils.py)), чтобы
   rename/hardlink оставались атомарными. Проверяйте пути через `is_within`.
4. **Один worker.** Startup-recovery и source-lock lease не распределённые. Не запускайте
   `docker compose up --scale worker=2` и не добавляйте второй worker без переработки координации.
5. **Lease.** При потере source-lock worker обязан прекратить запись (`LeaseLostError`), а не
   продолжать. Heartbeat и проверки `owns_lock` — часть контракта.
6. **Типизированные ошибки.** Новые сбои оформляйте наследниками `MediaLoaderError`
   ([errors.py](src/media_loader/errors.py)) с `http_status` и `code`; worker сохраняет их в job,
   API отдаёт клиенту `4xx/5xx` + `error_code`. Не глотайте исключения молча.
7. **Auth whitelist.** Публичные пути перечислены явно в `auth_middleware`: `/`, `/ui`, `/ui/config`,
   `/health`, `/static/*`, `/downloads/*`. Новый публичный маршрут требует правки этого списка;
   `/docs` и `/openapi.json` защищены авторизацией.
8. **Настройки.** Новый параметр — поле в `Settings` ([config.py](src/media_loader/config.py)),
   при необходимости валидатор длительности/размера (`24h`, `5GB`, `256KiB`), строка в
   [.env.example](.env.example) и таблица в README. Compose пробрасывает `.env` через `env_file`.
9. **Async-границы.** FastAPI и arq асинхронные. Блокирующие вызовы (yt-dlp, синхронный `httpx`,
   `sqlite3`, синхронный Redis прогресса, `shutil`) запускайте через `asyncio.to_thread`; ffmpeg и
   ffprobe — через `asyncio.create_subprocess_exec`. Блокировка event loop ломает polling.
10. **Прогресс ffmpeg.** Команды должны содержать `-progress pipe:1 -nostats`; `run_ffmpeg` парсит
    `out_time=` и `progress=end`. Сохраняйте этот контракт при изменении команд.
11. **Идемпотентность job'ов.** Создание через `create_job_nx`, arq job id = `<request_key>:<token>`,
    устаревшие токены игнорируются. Не ломайте эту защиту от дублей и гонок.
12. **Один публичный медиа-эндпоинт.** `POST /videos`; не добавляйте отдельные маршруты под форматы
    или качество без явной необходимости.
13. **Коллекции — это набор обычных job'ов.** Каждая серия проходит тот же source-кэш и ffmpeg-пайплайн;
    не делайте отдельный кэш «сезонов» и не склеивайте файлы. Манифест — JSON, файлы — отдельные
    `/downloads/<token>` с `X-Media-Season`/`X-Media-Episode`. Выбор серий (`episodes`) применяется
    после резолва, но до создания коллекции, и обязан попадать в `collection_key`.
14. **TTL коллекции.** Дочерние job'ы получают `ttl = COLLECTION_TTL` и `expires_at`;
    `cleanup_jobs` обязан уважать `expires_at` и не удалять их по обычному `JOB_TTL`.
15. **Разрешение сериала.** Универсальный путь — yt-dlp `extract_flat` для плейлистов/сезонов.
    RuTube-адаптер нужен, потому что из ссылки на серию yt-dlp не отдаёт родителя и номера серий.
    Для канальных загрузок сначала ищется TV-сущность (`/api/metainfo/tv/?search=`) и берутся её видео;
    если её нет — search-fallback по названию и автору с разбором `N сезон M серия`.
    VK: у платформы нет нативных сезонов; используйте playlists/community-разбор, не выдумывайте серии
    из марафонов. Это эвристика, не превращайте её в безлимитный обход канала.
    Не ходите в произвольные URL из резолвера.

## Карта модулей

| Файл | Что внутри |
|---|---|
| [api.py](src/media_loader/api.py) | `create_app()`, auth-middleware, `/videos`, `/health`, `/ui*`, `/downloads/*`, метаданные в заголовках |
| [worker.py](src/media_loader/worker.py) | `WorkerSettings`, `process_request`, `ensure_source`, direct/proxy fallback, `_derive`, cleanup-cron, startup-recovery |
| [ytdlp.py](src/media_loader/ytdlp.py) | опции yt-dlp, probe/download, извлечение метаданных, классификация retryable-ошибок, format selector |
| [transcode.py](src/media_loader/transcode.py) | `probe_media`, `build_output_plan`, команды ffmpeg, `run_ffmpeg` |
| [netcheck.py](src/media_loader/netcheck.py) | TLS-probe, выбор direct/proxy, metadata-only fallback |
| [proxy_strategies.py](src/media_loader/proxy_strategies.py) | парсинг файла стратегий, перебор, Redis-хранилище и лок |
| [domain_state.py](src/media_loader/domain_state.py) | состояние `direct`/`proxy` на host |
| [store.py](src/media_loader/store.py) | весь Redis-доступ: source/job, TTL/LRU, локи, download-токены, recovery |
| [metadata_db.py](src/media_loader/metadata_db.py) | SQLite JSON-хранилище (WAL, один коннект + lock) |
| [canonical.py](src/media_loader/canonical.py) | канонизация URL (YouTube-варианты, tracking-параметры), ключи |
| [series.py](src/media_loader/series.py) | разрешение сезонов/серий: generic yt-dlp + RuTube API + VK playlists/community |
| [collections.py](src/media_loader/collections.py) | ключи коллекций, записи в Redis, кэш сериалов, recovery |
| [quality.py](src/media_loader/quality.py) | quality ladder, clamp по source, масштабирование |
| [progress.py](src/media_loader/progress.py) | throttled-репортер и hook для yt-dlp |
| [auth.py](src/media_loader/auth.py) | токены (SHA-256) и CLI `media-loader-auth` |
| [admin.py](src/media_loader/admin.py) | CLI `media-loader-admin`: `cache-stats`, `proxy-reset`, `proxy-strategies` |
| [healthcheck.py](src/media_loader/healthcheck.py) | CLI для Docker healthcheck |
| [schemas.py](src/media_loader/schemas.py) | `VideoRequest`, допустимые форматы, `resolve_request` |
| [config.py](src/media_loader/config.py) | pydantic-settings, парсинг длительностей/размеров, пути |
| [errors.py](src/media_loader/errors.py) | типизированные ошибки и HTTP-коды |
| [utils.py](src/media_loader/utils.py) | время, безопасные имена, atomic move, content types |
| [static/](src/media_loader/static) | UI: `index.html`, `styles.css`, `app.js`, `favicon.svg` |

## Типовые изменения

- **Новая настройка:** `Settings` → `.env.example` → README (таблица) → при необходимости
  `compose.yaml` → тест в `tests/test_config.py`.
- **Новый формат/качество:** `schemas.py` (множества форматов), `transcode.build_output_plan` и
  команды, `utils.CONTENT_TYPES`; UI получает списки из `/ui/config`, но в `app.js` есть
  `fallbackConfig` — синхронизируйте его; обновите README.
- **Новый эндпоинт:** обработчик внутри `create_app` (не создавайте клиентов Redis/arq на import
  time — тесты инжектят их через фабрику), при публичности — whitelist в middleware, тесты в
  `tests/test_api.py`.
- **Новая ошибка:** наследник `MediaLoaderError`, маппинг в worker/API, тест на код ответа.
- **Новый источник сезонов:** добавьте адаптер в `series.py` (фиксированный API, не произвольный URL),
  тесты с замоканным клиентом, обновите README/AGENTS и лимиты.
- **Изменение Redis-раскладки:** константы/методы в `store.py`, все места удаления и
  `recover_incomplete_jobs`, `admin.py`, `tests/test_store.py`.
- **Изменение схемы SQLite:** `metadata_db.py` (`CREATE TABLE IF NOT EXISTS`), README (бэкапы,
  ручная миграция), тест. Автоматических миграций нет.
- **UI:** правится только `static/`; после правок — `deno fmt`, `deno lint`, `deno check`.
  Токен хранится в `localStorage`, polling — повторный `POST` с `respond_json=true`.
- **byedpi:** строки в [config/byedpi-strategies.conf](config/byedpi-strategies.conf) задают
  стратегии и **порядок портов** (`BYEDPI_BASE_PORT + индекс`); порт в `PROXY_URL` должен совпадать
  с базовым. Сброс состояния — `media-loader-admin proxy-reset <host>`.

## Тесты: правила и паттерны

- Файлы — `tests/test_<модуль>.py`; `asyncio_mode = "auto"`, поэтому async-тестам не нужен маркер.
- Фикстура `settings` ([tests/conftest.py](tests/conftest.py)) даёт временный `data_dir`, выключенный
  auth и маленькие лимиты. Не используйте реальный `/data`.
- Redis — `fakeredis.aioredis.FakeRedis(decode_responses=True)`; API — `create_app(...)` с
  инжектированными redis/arq/`MetadataDatabase`, `httpx.ASGITransport` и `app.router.lifespan_context`.
- Внешние вызовы (yt-dlp, ffprobe, httpx) подменяются monkeypatch'ем; реальные загрузки в тестах
  не выполняются.
- ffmpeg/ffprobe-тесты реальные и пропускаются, если бинарников нет. Сохраняйте `skip`, чтобы CI и
  локальная машина без ffmpeg оставались зелёными.
- Коллекции: `tests/test_series.py` (резолвер) и `tests/test_collections.py` (store, worker, API
  с `FakeArq`); не ходите в реальный RuTube/VK/yt-dlp из тестов — VK/RuTube-клиенты подменяются.
- Покрытие — branch coverage, порог 80%; новая ветка логики должна быть покрыта.

## Стиль кода

- `from __future__ import annotations` — первой строкой каждого модуля, включая тесты.
- Ruff: line length 100, широкий набор правил (ANN/ARG/ASYNC/B/SIM/RUF/PTH/...). Тесты
  освобождены от `ANN`, `ARG`, `PT`.
- Mypy `strict` для `src` и `tests`; pyright `strict` для `src` (часть `reportUnknown*` отключена
  из-за динамических словарей yt-dlp/Redis). Явный `Any` допустим на внешних границах.
- Комментарии, docstring'и, имена — английские; README, UI-тексты и пользовательские сообщения —
  русские. Соблюдайте это разделение.
- Логирование — модульный `LOGGER`; `print` только в CLI (`auth`, `admin`, `healthcheck`).
- Внутренние структуры — dataclass'ы; записи в Redis — JSON-словари (сложившаяся практика).

## Docker и эксплуатация

```bash
cp .env.example .env
docker compose config
docker compose build
docker compose up -d
docker compose ps
docker compose exec -T api media-loader-auth create --name dev --quiet
```

- `api` публикуется только на `127.0.0.1:${API_PORT}`; наружу — через TLS reverse proxy.
- Публикация в `docker.ithub24.ru` — локально через [scripts/publish-image.sh](scripts/publish-image.sh)
  (`TAG`, `PLATFORMS`, `BUILDER`); GitHub Actions не имеет доступа к LAN-registry. Multi-arch требует
  buildx-builder с нативными узлами amd64 + arm64: QEMU падает на `uv sync`. Не добавляйте в образ
  зависимости, доступные только под одну архитектуру (ffmpeg ставится из apt, Deno — из wheel);
  `docker compose build` собирает только текущую архитектуру.
- `api` ждёт healthy `redis` и запущенный `byedpi`; `worker` — один экземпляр, `stop_grace_period: 1m`.
- `HOST_DATA_DIR` монтируется в `/data`, `./config` — read-only в `/config`. Контейнер работает от
  UID/GID 1000, каталог данных должен быть доступен этому пользователю.
- `/health` проверяет Redis, SQLite и TCP-доступность byedpi; CLI `media-loader-healthcheck` —
  то же для Docker healthcheck.
- Бэкап SQLite — через backup API/`VACUUM INTO`, не `cp` при работающем WAL. Redis volume содержит
  токены (при потере токены нужно создавать заново).
- Не коммитьте `.env`, `config/cookies.txt`, `data/`, `dist/`, `.coverage` — они в `.gitignore`.
  Новый отслеживаемый файл в `config/` потребует правки `.gitignore`.

## Подводные камни

- `metadata_only=true` выполняется **в API синхронно** и может ходить в сеть (yt-dlp probe с
  fallback direct/proxy). Не превращайте его в job и не блокируйте event loop.
- `scope` по умолчанию — `episode`; не меняйте это без миграции клиентов. UI использует `auto`.
  Для коллекции `200` всегда JSON-манифест, а не файл. `episodes` допустим только для `season`
  и входит в `collection_key`; не теряйте его при polling.
- Универсально «любая ссылка на серию» не работает: yt-dlp не выводит родителя из одиночного видео.
  RuTube-адаптер закрывает нативные сериалы; playlist-серии требуют `?pl_id`/страницу плейлиста;
  канальные загрузки — через TV-сущность (`metainfo/tv?search=`) или, если её нет, по заголовку
  `N сезон M серия` (search-fallback). Спецвыпуски/OVA в сезоны не включаются.
- VK зависит от сообщества: плейлисты и заголовки могут быть неполными/смешанными; одиночный
  марафон не разрезается, а fallback по видео сообщества может быть медленным (тысячи роликов).
- Коллекции обрабатываются последовательно и переиспользуют source-кэш; не запускайте их параллельно
  в обход `max_concurrent_jobs` и не снимайте лимит `MAX_COLLECTION_EPISODES` без причины.
- Первый запрос к заблокированному домену медленный: TLS-probe + перебор стратегий byedpi.
  Это ожидаемо; не «исправляйте» таймаутами, ломающими fallback.
- Селектор форматов yt-dlp намеренно предпочитает прямые HTTP/DASH, а не HLS — HLS-фрагменты
  чаще рвутся при обходе DPI. Меняя селектор, проверяйте YouTube через byedpi.
- YouTube проходит через несколько player client'ов (`YTDLP_YOUTUBE_PLAYER_CLIENTS`); порядок значим.
- `QUALITY_LEVELS` — список допустимых меток; качество выше source не апскейлится, а зажимается
  (`quality_clamped=true`).
- Пути из job-записей всегда проверяйте на принадлежность каталогу (`is_within`): они приходят из
  Redis, а не из доверенного источника.
- Download-токен — секрет и capability URL: не логируйте его и не публикуйте ссылку (в UI он живёт
  в пути `/downloads/<token>`, а не в query).
- `get_settings()` кэширован (`lru_cache`), а `WorkerSettings` читает настройки на import time;
  в тестах создавайте `Settings(...)` напрямую, а не меняйте окружение после импорта.
- `README.md` большой и подробный, но числа/детали могут устаревать — сверяйтесь с кодом.
  Меняя поведение, API, переменные или раскладку данных, обновляйте README и этот файл.

## Чек-лист перед завершением задачи

- [ ] `ruff format --check`, `ruff check`, `mypy`, `pyright`, `deno`-проверки, `pytest` зелёные.
- [ ] На новое поведение есть тесты; покрытие не упало ниже порога.
- [ ] Обновлены README, `.env.example` и `AGENTS.md`, если изменились поведение/настройки/структура.
- [ ] Для коллекций проверены порядок серий, `S01E02`-имена, `X-Media-Season/Episode` и TTL.
- [ ] Не изменены формулы ключей, source-only-кэш, auth-whitelist и single-worker-допущения без
      осознанного решения и миграционного плана.
- [ ] В диффе нет секретов, `.env`, cookies, данных из `data/` и артефактов сборки.
