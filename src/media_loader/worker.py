from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar, cast

import redis.asyncio as aioredis
from arq import cron
from arq.connections import RedisSettings

from media_loader.auth import AUTH_NAMES_KEY
from media_loader.canonical import host_key
from media_loader.config import Settings, get_settings
from media_loader.domain_state import DomainState
from media_loader.errors import (
    DiskFullError,
    DownloadFailedError,
    LeaseLostError,
    MediaLoaderError,
    UnsupportedMediaError,
)
from media_loader.metadata_db import MetadataDatabase
from media_loader.netcheck import direct_available
from media_loader.progress import ProgressReporter
from media_loader.proxy_strategies import (
    ProxyStrategyStore,
    ensure_proxy_strategy,
    is_proxy_strategy_error,
)
from media_loader.store import Store
from media_loader.transcode import build_output_plan, probe_media, remux_to_mkv, run_ffmpeg
from media_loader.utils import (
    atomic_move,
    hardlink_or_copy,
    iso,
    parse_dt,
    safe_filename,
    utcnow,
)
from media_loader.ytdlp import (
    DownloadResult,
    download_source,
    is_retryable_error,
    metadata_to_dict,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttemptResult:
    result: DownloadResult
    via_proxy: bool
    proxy_strategy: str | None = None
    player_client: str | None = None


def _source_is_ready(source: dict[str, Any] | None) -> bool:
    if not source or source.get("status") != "ready":
        return False
    path = source.get("source_path")
    if not path or not Path(str(path)).is_file():
        return False
    expires = parse_dt(str(source.get("expires_at") or ""))
    return bool(expires and expires > utcnow())


def _clear_directory(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            with contextlib.suppress(FileNotFoundError):
                child.unlink()


async def _save_job(store: Store, job: dict[str, Any]) -> None:
    await store.save_job(job)


async def _fail_job(
    store: Store,
    job: dict[str, Any],
    exc: BaseException,
    http_status: int | None = None,
    code: str | None = None,
) -> None:
    if isinstance(exc, MediaLoaderError):
        status = exc.http_status
        error_code = exc.code
        message = exc.message
    else:
        status = http_status or 502
        error_code = code or "internal_error"
        message = str(exc)
    now = iso(utcnow())
    job.update(
        status="failed",
        stage="failed",
        error=message[-2000:],
        error_code=error_code,
        http_status=status,
        finished_at=now,
    )
    job["progress"] = {"percent": None, "stage": "failed"}
    await _save_job(store, job)
    await asyncio.to_thread(shutil.rmtree, store.job_dir(str(job["request_key"])), True)
    LOGGER.error("job %s failed: %s", job.get("request_key"), message)


async def _download_with_fallback(
    job: dict[str, Any],
    source_key: str,
    canonical_url: str,
    host: str,
    temp_dir: Path,
    settings: Settings,
    store: Store,
    domain_state: DomainState,
    reporter: ProgressReporter,
    cancel_event: threading.Event,
    proxy_strategies: ProxyStrategyStore | None = None,
) -> AttemptResult:
    if proxy_strategies is None:
        proxy_strategies = ProxyStrategyStore(store.redis, settings)
    mode = await domain_state.get_mode(host)
    if mode == "proxy":
        attempts: list[bool] = [True]
    elif mode is None and not await direct_available(
        canonical_url,
        settings.direct_probe_timeout,
    ):
        await domain_state.set(host, "proxy", "direct TLS probe failed")
        attempts = [True]
    else:
        attempts = [False, True]
    last_error: BaseException | None = None

    for index, use_proxy in enumerate(attempts, start=1):
        job.update(
            stage="downloading",
            via_proxy=use_proxy,
            attempts=index,
            download_started_at=job.get("download_started_at") or iso(utcnow()),
        )
        await _save_job(store, job)
        reporter.clear_source(source_key)
        reporter.job(
            str(job["request_key"]),
            stage="downloading",
            percent=0,
            _force=True,
        )
        if temp_dir.exists():
            await asyncio.to_thread(_clear_directory, temp_dir)
        result: DownloadResult
        selected_strategy: str | None = None
        selected_client: str | None = None
        if use_proxy:
            try:
                result, strategy = await _download_with_proxy_strategy(
                    job,
                    source_key,
                    canonical_url,
                    host,
                    temp_dir,
                    settings,
                    store,
                    proxy_strategies,
                    reporter,
                    cancel_event,
                )
            except (UnsupportedMediaError, LeaseLostError):
                raise
            except MediaLoaderError:
                raise
            except Exception as exc:
                last_error = exc
                if mode == "proxy" and (
                    "connection refused" in str(exc).lower()
                    or "name or service not known" in str(exc).lower()
                ):
                    await domain_state.delete(host)
                raise DownloadFailedError(str(exc)) from exc
            selected_strategy = str(strategy.get("strategy") or "")
            selected_client = cast(str | None, strategy.get("player_client"))
        else:
            try:
                result = await asyncio.to_thread(
                    download_source,
                    canonical_url,
                    temp_dir,
                    None,
                    settings,
                    reporter,
                    source_key,
                    fast_fail=True,
                    cancel_event=cancel_event,
                )
            except (UnsupportedMediaError, LeaseLostError):
                raise
            except Exception as exc:
                last_error = exc
                if not is_retryable_error(exc):
                    raise DownloadFailedError(str(exc)) from exc
                LOGGER.warning("direct attempt failed for %s: %s", canonical_url, exc)
                await domain_state.set(host, "proxy", f"direct failed: {exc}")
                continue

        await domain_state.set(
            host,
            "proxy" if use_proxy else "direct",
            (
                f"proxy download succeeded via {selected_strategy}"
                if use_proxy
                else "direct download succeeded"
            ),
        )
        return AttemptResult(
            result=result,
            via_proxy=use_proxy,
            proxy_strategy=selected_strategy or None,
            player_client=selected_client,
        )

    raise DownloadFailedError(str(last_error) if last_error else "download failed")


async def _download_with_proxy_strategy(
    job: dict[str, Any],
    source_key: str,
    canonical_url: str,
    host: str,
    temp_dir: Path,
    settings: Settings,
    store: Store,
    proxy_strategies: ProxyStrategyStore,
    reporter: ProgressReporter,
    cancel_event: threading.Event,
) -> tuple[DownloadResult, dict[str, Any]]:
    strategy = await proxy_strategies.get(host)
    if strategy is None:
        job.update(stage="probing_proxy")
        await _save_job(store, job)
        reporter.job(
            str(job["request_key"]),
            stage="probing_proxy",
            percent=0,
            _force=True,
        )
        strategy = await ensure_proxy_strategy(canonical_url, host, settings, proxy_strategies)

    last_error: BaseException | None = None
    for attempt in range(3):
        job.update(
            stage="downloading",
            proxy_strategy=strategy.get("strategy"),
            player_client=strategy.get("player_client"),
        )
        await _save_job(store, job)
        reporter.job(
            str(job["request_key"]),
            stage="downloading",
            percent=0,
            _force=True,
        )
        if temp_dir.exists():
            await asyncio.to_thread(_clear_directory, temp_dir)
        try:
            result = await asyncio.to_thread(
                download_source,
                canonical_url,
                temp_dir,
                str(strategy["proxy_url"]),
                settings,
                reporter,
                source_key,
                fast_fail=False,
                cancel_event=cancel_event,
                player_client=cast(str | None, strategy.get("player_client")),
            )
            return result, strategy
        except (UnsupportedMediaError, LeaseLostError):
            raise
        except Exception as exc:
            last_error = exc
            if not is_proxy_strategy_error(exc):
                raise
            if attempt == 0:
                LOGGER.warning(
                    "transient proxy failure for %s, retrying same strategy: %s",
                    host,
                    exc,
                )
                continue
            await proxy_strategies.delete(host)
            if attempt == 1:
                LOGGER.warning("proxy strategy failed for %s, probing another: %s", host, exc)
                job.update(stage="probing_proxy")
                await _save_job(store, job)
                reporter.job(
                    str(job["request_key"]),
                    stage="probing_proxy",
                    percent=0,
                    _force=True,
                )
                strategy = await ensure_proxy_strategy(
                    canonical_url,
                    host,
                    settings,
                    proxy_strategies,
                )
                continue
            raise
    raise DownloadFailedError(str(last_error) if last_error else "proxy download failed")


async def _store_source(
    job: dict[str, Any],
    source_key: str,
    canonical_url: str,
    host: str,
    settings: Settings,
    store: Store,
    domain_state: DomainState,
    reporter: ProgressReporter,
    metadata_db: MetadataDatabase,
    lock_name: str,
    lock_token: str,
    cancel_event: threading.Event,
    proxy_strategies: ProxyStrategyStore | None = None,
) -> dict[str, Any]:
    temp_dir = Path(tempfile.mkdtemp(prefix=f"source-{source_key}-", dir=settings.tmp_dir))
    now = utcnow()
    record: dict[str, Any] = {
        "source_key": source_key,
        "url": job["url"],
        "canonical_url": canonical_url,
        "status": "downloading",
        "created_at": iso(now),
        "download_started_at": iso(now),
        "finished_at": None,
        "expires_at": None,
        "source_path": None,
        "source_filename": None,
        "source_ext": None,
        "source_size": 0,
        "has_video": False,
        "width": None,
        "height": None,
        "source_quality": None,
        "video_codec": None,
        "audio_codec": None,
        "duration": None,
        "title": None,
        "thumbnail": None,
        "description": None,
        "available_qualities": [],
        "metadata_details": {},
        "via_proxy": False,
        "attempts": 0,
        "error": None,
    }
    await store.save_source(record)
    if shutil.disk_usage(settings.data_dir).free < settings.min_free_disk:
        record.update(
            status="failed",
            finished_at=iso(utcnow()),
            error="not enough free disk space for source download",
        )
        await store.save_source(record)
        raise DiskFullError("not enough free disk space for source download")
    job.update(
        status="downloading_source",
        stage="downloading",
        download_started_at=iso(now),
    )
    await _save_job(store, job)

    try:
        attempt = await _download_with_fallback(
            job,
            source_key,
            canonical_url,
            host,
            temp_dir,
            settings,
            store,
            domain_state,
            reporter,
            cancel_event,
            proxy_strategies,
        )
        downloaded = await probe_media(attempt.result.path, settings)
        if downloaded.has_video:
            staged = temp_dir / "source.mkv"
            if attempt.result.path.suffix.lower() == ".mkv":
                if attempt.result.path != staged:
                    await asyncio.to_thread(atomic_move, attempt.result.path, staged)
            else:
                await remux_to_mkv(attempt.result.path, staged, settings)
        else:
            ext = attempt.result.path.suffix.lower().lstrip(".") or "m4a"
            staged = temp_dir / f"source.{ext}"
            if attempt.result.path != staged:
                await asyncio.to_thread(atomic_move, attempt.result.path, staged)

        if cancel_event.is_set() or not await store.owns_lock(lock_name, lock_token):
            raise LeaseLostError
        media = await probe_media(staged, settings)
        cache_dir = store.source_dir(source_key)
        await asyncio.to_thread(cache_dir.mkdir, parents=True, exist_ok=True)
        destination = cache_dir / staged.name
        await asyncio.to_thread(atomic_move, staged, destination)
        meta = attempt.result.metadata
        title = meta.title
        ext = destination.suffix.lower().lstrip(".") or "mkv"
        finished = utcnow()
        record.update(
            status="ready",
            finished_at=iso(finished),
            expires_at=iso(finished + timedelta(seconds=settings.cache_ttl)),
            source_path=str(destination),
            source_filename=safe_filename(title, source_key, f".{ext}"),
            source_ext=ext,
            source_size=destination.stat().st_size,
            has_video=media.has_video,
            width=media.width,
            height=media.height,
            source_quality=(
                f"{min(media.width, media.height)}p" if media.width and media.height else None
            ),
            video_codec=media.video_codec,
            audio_codec=media.audio_codec,
            duration=media.duration or meta.duration,
            title=title,
            thumbnail=meta.thumbnail,
            description=meta.description,
            available_qualities=list(meta.available_qualities),
            metadata_details=meta.details,
            via_proxy=attempt.via_proxy,
            proxy_strategy=attempt.proxy_strategy,
            player_client=attempt.player_client,
            attempts=int(job.get("attempts") or 1),
            error=None,
        )
        await store.save_source(record)
        await store.mark_source_ready(record)
        db_metadata = metadata_to_dict(meta)
        db_metadata.update(
            {
                "has_video": media.has_video,
                "has_audio": media.has_audio,
                "width": media.width,
                "height": media.height,
                "max_quality": record["source_quality"],
                "available_qualities": record.get("available_qualities") or [],
                "video_codec": media.video_codec,
                "audio_codec": media.audio_codec,
                "duration": media.duration or meta.duration,
            }
        )
        try:
            await metadata_db.upsert(source_key, canonical_url, db_metadata)
        except Exception:
            LOGGER.exception("failed to save metadata to SQLite")
        job.update(
            via_proxy=attempt.via_proxy,
            proxy_strategy=attempt.proxy_strategy,
            player_client=attempt.player_client,
            attempts=record["attempts"],
            title=title,
            duration=record["duration"],
            thumbnail=record["thumbnail"],
            description=record.get("description"),
            available_qualities=record.get("available_qualities") or [],
            metadata_details=record.get("metadata_details") or {},
        )
        reporter.job(str(job["request_key"]), stage="merging", percent=100, _force=True)
        return record
    except LeaseLostError:
        raise
    except Exception as exc:
        if cancel_event.is_set() or not await store.owns_lock(lock_name, lock_token):
            raise LeaseLostError from exc
        record.update(
            status="failed",
            finished_at=iso(utcnow()),
            error=str(exc)[-2000:],
            via_proxy=bool(job.get("via_proxy")),
            attempts=int(job.get("attempts") or 1),
        )
        await store.save_source(record)
        await asyncio.to_thread(shutil.rmtree, store.source_dir(source_key), True)
        raise
    finally:
        await asyncio.to_thread(shutil.rmtree, temp_dir, True)


async def _renew_lease(
    store: Store,
    lock_name: str,
    token: str,
    ttl: int,
    cancel_event: threading.Event,
) -> None:
    interval = max(1, ttl // 3)
    while True:
        await asyncio.sleep(interval)
        try:
            if not await store.renew_lock(lock_name, token, ttl):
                LOGGER.warning("source lock lease lost: %s", lock_name)
                cancel_event.set()
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("failed to renew source lock %s", lock_name)


async def ensure_source(
    job: dict[str, Any],
    source_key: str,
    canonical_url: str,
    settings: Settings,
    store: Store,
    domain_state: DomainState,
    reporter: ProgressReporter,
    metadata_db: MetadataDatabase,
    proxy_strategies: ProxyStrategyStore | None = None,
) -> dict[str, Any]:
    host = host_key(canonical_url)
    deadline = time.monotonic() + settings.job_timeout
    while True:
        source = await store.get_source(source_key)
        if _source_is_ready(source):
            assert source is not None
            await store.mark_source_access(source_key)
            return source
        if source and source.get("status") in {"failed", "ready"}:
            await store.delete_source(source_key, force=True)

        lock_name = store.source_lock_name(source_key)
        lock = await store.acquire_lock(lock_name, settings.source_lock_ttl)
        if lock:
            cancel_event = threading.Event()
            heartbeat = asyncio.create_task(
                _renew_lease(
                    store,
                    lock_name,
                    lock,
                    settings.source_lock_ttl,
                    cancel_event,
                )
            )
            try:
                source = await store.get_source(source_key)
                if _source_is_ready(source):
                    assert source is not None
                    await store.mark_source_access(source_key)
                    return source
                if source:
                    await store.delete_source(source_key, force=True)
                return await _store_source(
                    job,
                    source_key,
                    canonical_url,
                    host,
                    settings,
                    store,
                    domain_state,
                    reporter,
                    metadata_db,
                    lock_name,
                    lock,
                    cancel_event,
                    proxy_strategies,
                )
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
                await store.release_lock(lock_name, lock)

        job.update(status="waiting_for_source", stage="waiting")
        if source and source.get("download_started_at"):
            job["download_started_at"] = source["download_started_at"]
        await _save_job(store, job)
        LOGGER.info("job %s waiting for source %s", job["request_key"], source_key)

        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            source = await store.get_source(source_key)
            if _source_is_ready(source):
                assert source is not None
                await store.mark_source_access(source_key)
                return source
            if source and source.get("status") in {"failed", "ready"}:
                break
            if not await store.redis.exists(store.lock_key(store.source_lock_name(source_key))):
                break
        else:
            raise DownloadFailedError("timed out waiting for source download")


def _title_for_output(title: str, media_type: str, actual_quality: str | None, suffix: str) -> str:
    base = title
    if media_type == "video" and actual_quality:
        base = f"{base} [{actual_quality}]"
    return safe_filename(base, "media", f".{suffix}")


async def _derive(
    job: dict[str, Any],
    source: dict[str, Any],
    settings: Settings,
    store: Store,
    reporter: ProgressReporter,
) -> None:
    source_path = Path(str(source["source_path"]))
    info = await probe_media(source_path, settings)
    media_type = str(job["media_type"])
    output_format = str(job["format"])
    requested_quality = job.get("quality")
    job_dir = store.job_dir(str(job["request_key"]))
    await asyncio.to_thread(shutil.rmtree, job_dir, True)
    await asyncio.to_thread(job_dir.mkdir, parents=True, exist_ok=True)

    provisional = job_dir / "output"
    plan = build_output_plan(
        settings,
        info,
        media_type,
        output_format,
        str(requested_quality) if requested_quality else None,
        provisional,
    )
    filename = _title_for_output(
        str(source.get("title") or "media"),
        media_type,
        plan.actual_quality,
        plan.suffix,
    )
    output_path = job_dir / filename
    if plan.copy_source:
        await asyncio.to_thread(hardlink_or_copy, source_path, output_path)
    elif plan.command:
        source_size = source_path.stat().st_size
        if shutil.disk_usage(settings.job_dir).free < max(settings.min_free_disk, source_size):
            raise DiskFullError("not enough free disk space for derivation")
        plan.command[-1] = str(output_path)
        await run_ffmpeg(
            plan.command,
            info.duration,
            reporter,
            str(job["request_key"]),
            plan.stage,
        )

    size = output_path.stat().st_size
    now = iso(utcnow())
    job.update(
        status="ready",
        stage="ready",
        source_cached=bool(job.get("source_cached")),
        actual_quality=plan.actual_quality,
        source_quality=plan.source_quality,
        quality_clamped=plan.quality_clamped,
        output_path=str(output_path),
        output_filename=filename,
        content_type=plan.content_type,
        size=size,
        width=plan.width,
        height=plan.height,
        video_codec=plan.video_codec,
        audio_codec=plan.audio_codec,
        finished_at=now,
        error=None,
        error_code=None,
        http_status=200,
    )
    job["progress"] = {"percent": 100.0, "stage": "ready"}
    await store.save_job(job, ttl=settings.job_ttl)
    reporter.job(str(job["request_key"]), stage="ready", percent=100, _force=True)


async def process_request(
    ctx: dict[str, Any],
    request_key: str,
    arq_token: str | None = None,
) -> None:
    settings: Settings = ctx["settings"]
    store: Store = ctx["store"]
    domain_state: DomainState = ctx["domain_state"]
    proxy_strategies = ctx.get("proxy_strategies")
    if not isinstance(proxy_strategies, ProxyStrategyStore):
        proxy_strategies = ProxyStrategyStore(store.redis, settings)
    transcode_sem: asyncio.Semaphore = ctx["transcode_sem"]
    metadata_db: MetadataDatabase = ctx["metadata_db"]
    job = await store.get_job(request_key)
    if not job:
        LOGGER.warning("job %s disappeared before processing", request_key)
        return
    if arq_token and job.get("arq_token") and arq_token != job["arq_token"]:
        LOGGER.info("stale arq job %s ignored", request_key)
        return
    if job.get("status") in {"ready", "failed"}:
        return

    source_key = str(job["source_key"])
    canonical_url = str(job["canonical_url"])
    reporter = ProgressReporter(settings)
    await store.incr_active(source_key)
    try:
        source = await ensure_source(
            job,
            source_key,
            canonical_url,
            settings,
            store,
            domain_state,
            reporter,
            metadata_db,
            proxy_strategies,
        )
        await store.mark_source_access(source_key)
        now = iso(utcnow())
        job.update(
            status="processing",
            stage="processing",
            processing_started_at=now,
            source_quality=source.get("source_quality"),
            title=job.get("title") or source.get("title"),
            duration=job.get("duration") or source.get("duration"),
            thumbnail=job.get("thumbnail") or source.get("thumbnail"),
            description=job.get("description") or source.get("description"),
            available_qualities=source.get("available_qualities") or [],
            metadata_details=source.get("metadata_details") or {},
            via_proxy=source.get("via_proxy", job.get("via_proxy")),
            attempts=source.get("attempts", job.get("attempts")),
        )
        await _save_job(store, job)
        async with transcode_sem:
            await _derive(job, source, settings, store, reporter)
    except MediaLoaderError as exc:
        await _fail_job(store, job, exc)
    except Exception as exc:
        LOGGER.exception("unexpected job error")
        await _fail_job(store, job, exc)
    finally:
        reporter.close()
        await store.decr_active(source_key)


async def cleanup_task(ctx: dict[str, Any]) -> None:
    store: Store = ctx["store"]
    try:
        sources = await store.cleanup_sources()
        jobs = await store.cleanup_jobs()
        LOGGER.info("cleanup: sources=%s jobs_removed=%s", sources, jobs)
    except Exception:
        LOGGER.exception("cleanup failed")


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    settings.ensure_dirs()
    redis: Any = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
    ctx["settings"] = settings
    ctx["redis"] = redis
    ctx["store"] = Store(redis, settings)
    ctx["domain_state"] = DomainState(redis, settings)
    ctx["proxy_strategies"] = ProxyStrategyStore(redis, settings)
    ctx["metadata_db"] = MetadataDatabase(settings.metadata_db_path)
    ctx["transcode_sem"] = asyncio.Semaphore(settings.max_concurrent_transcodes)
    recovered = await ctx["store"].recover_incomplete_jobs()
    if recovered:
        LOGGER.warning("recovered %s incomplete jobs after worker restart", recovered)
    if settings.auth_enabled and await redis.hlen(AUTH_NAMES_KEY) == 0:
        LOGGER.warning(
            "no API tokens configured; create one with media-loader-auth create --name ..."
        )


async def shutdown(ctx: dict[str, Any]) -> None:
    metadata_db = ctx.get("metadata_db")
    if metadata_db is not None:
        await metadata_db.close()
    redis = ctx.get("redis")
    if redis is not None:
        await redis.aclose()


def _cleanup_schedule(interval: int) -> tuple[set[int] | None, set[int]]:
    if interval >= 3600:
        step = max(1, round(interval / 3600))
        return set(range(0, 24, step)), {0}
    step = max(1, round(interval / 60))
    return None, set(range(0, 60, step))


class WorkerSettings:
    settings = get_settings()
    functions: ClassVar[list[Any]] = [process_request]
    cleanup_hour, cleanup_minute = _cleanup_schedule(settings.cache_cleanup_interval)
    cron_jobs: ClassVar[list[Any]] = [
        cron(
            cleanup_task,
            hour=cleanup_hour,
            minute=cleanup_minute,
            run_at_startup=True,
        )
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.max_concurrent_jobs
    job_timeout = settings.job_timeout
    keep_result = 0
    max_tries = 1
    health_check_interval = 30
