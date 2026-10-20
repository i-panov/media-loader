from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from redis.exceptions import RedisError

from media_loader.auth import authenticate_async
from media_loader.canonical import canonicalize_url, host_key, request_key, source_key
from media_loader.config import Settings, get_settings
from media_loader.domain_state import DomainState
from media_loader.errors import InvalidRequestError, MediaLoaderError
from media_loader.metadata_db import MetadataDatabase
from media_loader.netcheck import fetch_metadata_with_fallback
from media_loader.quality import plan_quality, quality_height
from media_loader.schemas import (
    AUDIO_FORMATS,
    VIDEO_FORMATS,
    VideoRequest,
    resolve_request,
)
from media_loader.store import Store, job_progress_key, source_progress_key
from media_loader.utils import (
    is_within,
    iso,
    local_display,
    parse_dt,
    proxy_host_port,
    utcnow,
)
from media_loader.ytdlp import metadata_to_dict

LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _job_progress_payload(progress: dict[str, Any], fallback_stage: str) -> dict[str, Any]:
    return {
        "percent": progress.get("percent"),
        "stage": progress.get("stage") or fallback_stage,
        "downloaded_bytes": progress.get("downloaded_bytes"),
        "total_bytes": progress.get("total_bytes"),
        "speed_bps": progress.get("speed_bps"),
        "eta_seconds": progress.get("eta_seconds"),
    }


def _started_at(job: dict[str, Any]) -> str | None:
    for value in (
        job.get("download_started_at"),
        job.get("processing_started_at"),
        job.get("created_at"),
    ):
        if value:
            return str(value)
    return None


async def _status_payload(
    store: Store,
    settings: Settings,
    job: dict[str, Any],
) -> dict[str, Any]:
    status = str(job.get("status") or "queued")
    stage = str(job.get("stage") or status)
    if status in {"waiting_for_source", "downloading_source"}:
        progress = await store.get_progress(source_progress_key(str(job["source_key"])))
    else:
        progress = await store.get_progress(job_progress_key(str(job["request_key"])))
    if not progress and job.get("progress"):
        progress = dict(job["progress"])
    started = _started_at(job)
    started_dt = parse_dt(started)
    details = job.get("metadata_details") or {}
    return {
        "status": status,
        "stage": stage,
        "source_key": job.get("source_key"),
        "source_cached": bool(job.get("source_cached")),
        "media_type": job.get("media_type"),
        "format": job.get("format"),
        "available_qualities": job.get("available_qualities") or [],
        "requested_quality": job.get("quality"),
        "actual_quality": job.get("actual_quality"),
        "source_quality": job.get("source_quality"),
        "quality_clamped": bool(job.get("quality_clamped")),
        "download_started_at": job.get("download_started_at"),
        "processing_started_at": job.get("processing_started_at"),
        "started_at": started,
        "started_at_local": local_display(started_dt, settings.timezone),
        "timezone": settings.server_timezone,
        "progress": _job_progress_payload(progress, stage),
        "title": job.get("title"),
        "description": job.get("description") or details.get("description"),
        "filename": job.get("output_filename"),
        "content_type": job.get("content_type"),
        "size": job.get("size"),
        "duration": job.get("duration"),
        "thumbnail": job.get("thumbnail"),
        "uploader": details.get("uploader"),
        "channel": details.get("channel"),
        "upload_date": details.get("upload_date"),
        "view_count": details.get("view_count"),
        "like_count": details.get("like_count"),
        "tags": details.get("tags") or [],
        "categories": details.get("categories") or [],
        "via_proxy": bool(job.get("via_proxy")),
        "attempts": int(job.get("attempts") or 0),
        "error": job.get("error"),
        "error_code": job.get("error_code"),
        "retry_after": settings.retry_after_seconds,
    }


def _file_response(
    path: Path,
    media_type: str,
    filename: str,
    cache_header: str,
    **headers: str,
) -> FileResponse:
    if not path.is_file():
        raise HTTPException(status_code=404, detail="file is no longer available")
    response_headers = {"X-Cache": cache_header, **headers}
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        headers=response_headers,
    )


def _failed_response(job: dict[str, Any], settings: Settings) -> JSONResponse:
    status_code = int(job.get("http_status") or 502)
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "failed",
            "error": job.get("error") or "job failed",
            "error_code": job.get("error_code"),
            "retry_after": settings.failed_retry_seconds,
        },
        headers={"Retry-After": str(settings.failed_retry_seconds)},
    )


def _job_output_path(job: dict[str, Any], store: Store) -> Path | None:
    if job.get("status") != "ready":
        return None
    output_path = job.get("output_path")
    if not output_path:
        return None
    path = Path(str(output_path))
    if not path.is_file() or not is_within(store.job_dir(str(job["request_key"])), path):
        return None
    return path


async def _serve_job(
    job: dict[str, Any],
    store: Store,
) -> FileResponse | None:
    path = _job_output_path(job, store)
    if path is None:
        return None
    headers = _metadata_headers(_job_metadata(job))
    headers["X-Source-Key"] = str(job.get("source_key") or "")
    return _file_response(
        path,
        str(job.get("content_type") or "application/octet-stream"),
        str(job.get("output_filename") or path.name),
        "DERIVED",
        **headers,
    )


def _new_job_record(
    settings: Settings,
    resolved_media_type: str,
    resolved_format: str,
    quality: str | None,
    source: dict[str, Any] | None,
    canonical_url: str,
    key: str,
    source_key_value: str,
) -> dict[str, Any]:
    now = iso(utcnow())
    source_ready = bool(source and source.get("status") == "ready")
    return {
        "request_key": key,
        "source_key": source_key_value,
        "url": canonical_url,
        "canonical_url": canonical_url,
        "media_type": resolved_media_type,
        "format": resolved_format,
        "quality": quality,
        "source_cached": source_ready,
        "status": "queued",
        "stage": "queued",
        "created_at": now,
        "download_started_at": (
            source.get("download_started_at")
            if source and source.get("status") != "ready"
            else None
        ),
        "processing_started_at": None,
        "finished_at": None,
        "title": source.get("title") if source else None,
        "duration": source.get("duration") if source else None,
        "thumbnail": source.get("thumbnail") if source else None,
        "description": source.get("description") if source else None,
        "available_qualities": (source.get("available_qualities") or []) if source else [],
        "metadata_details": (source.get("metadata_details") or {}) if source else {},
        "source_quality": source.get("source_quality") if source else None,
        "actual_quality": None,
        "quality_clamped": False,
        "via_proxy": bool(source.get("via_proxy")) if source else False,
        "attempts": int(source.get("attempts") or 0) if source else 0,
        "error": None,
        "error_code": None,
        "http_status": None,
        "progress": {"percent": 0, "stage": "queued"},
        "retry_after": settings.retry_after_seconds,
    }


def _source_metadata(source: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(source.get("metadata_details") or {})
    metadata.update(
        {
            "title": source.get("title"),
            "description": source.get("description"),
            "duration": source.get("duration"),
            "thumbnail": source.get("thumbnail"),
            "has_video": bool(source.get("has_video")),
            "has_audio": source.get("audio_codec") is not None,
            "width": source.get("width"),
            "height": source.get("height"),
            "max_quality": source.get("source_quality"),
            "available_qualities": source.get("available_qualities")
            or ([source["source_quality"]] if source.get("source_quality") else []),
            "video_codec": source.get("video_codec"),
            "audio_codec": source.get("audio_codec"),
            "size": source.get("source_size"),
        }
    )
    return {key: value for key, value in metadata.items() if value is not None}


def _job_metadata(job: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(job.get("metadata_details") or {})
    metadata.update(
        {
            "title": job.get("title"),
            "description": job.get("description"),
            "duration": job.get("duration"),
            "thumbnail": job.get("thumbnail"),
            "has_video": job.get("media_type") == "video",
            "has_audio": job.get("audio_codec") is not None,
            "width": job.get("width"),
            "height": job.get("height"),
            "media_type": job.get("media_type"),
            "format": job.get("format"),
            "source_quality": job.get("source_quality"),
            "actual_quality": job.get("actual_quality"),
            "quality_clamped": bool(job.get("quality_clamped")),
            "video_codec": job.get("video_codec"),
            "audio_codec": job.get("audio_codec"),
            "size": job.get("size"),
            "available_qualities": job.get("available_qualities") or [],
        }
    )
    return {key: value for key, value in metadata.items() if value is not None}


def _actual_quality(
    resolved: Any,
    metadata: dict[str, Any],
    source: dict[str, Any] | None,
) -> tuple[str | None, str | None, bool]:
    source_quality = source.get("source_quality") if source else metadata.get("max_quality")
    width = source.get("width") if source else metadata.get("width")
    height = source.get("height") if source else metadata.get("height")
    if resolved.media_type != "video":
        return None, source_quality, False
    if width and height:
        plan = plan_quality(resolved.quality, int(width), int(height))
        return (
            plan.actual_quality or source_quality,
            plan.source_quality or source_quality,
            plan.quality_clamped,
        )
    if resolved.quality is None or not source_quality:
        return source_quality, source_quality, False
    requested = quality_height(resolved.quality)
    try:
        short_side = quality_height(str(source_quality))
    except ValueError:
        return source_quality, source_quality, False
    if requested >= short_side:
        return source_quality, source_quality, requested > short_side
    return f"{requested}p", source_quality, False


def _metadata_payload(
    resolved: Any,
    source_key_value: str,
    metadata: dict[str, Any],
    source: dict[str, Any] | None,
) -> dict[str, Any]:
    actual, source_quality, clamped = _actual_quality(resolved, metadata, source)
    payload = dict(metadata)
    payload.update(
        {
            "status": "ok",
            "metadata_only": True,
            "source_key": source_key_value,
            "source_cached": bool(source and source.get("status") == "ready"),
            "media_type": resolved.media_type,
            "format": resolved.output_format,
            "requested_quality": resolved.quality,
            "actual_quality": actual,
            "source_quality": source_quality,
            "quality_clamped": clamped,
            "size": source.get("source_size") if source else None,
            "expires_at": source.get("expires_at") if source else None,
        }
    )
    return {key: value for key, value in payload.items() if value is not None}


def _metadata_headers(metadata: dict[str, Any]) -> dict[str, str]:
    description = metadata.get("description")
    if isinstance(description, str) and len(description) > 500:
        description = f"{description[:500]}..."
    summary = {
        "id": metadata.get("id") or metadata.get("display_id"),
        "title": metadata.get("title"),
        "description": description,
        "duration": metadata.get("duration"),
        "uploader": metadata.get("uploader"),
        "channel": metadata.get("channel"),
        "upload_date": metadata.get("upload_date"),
        "view_count": metadata.get("view_count"),
        "like_count": metadata.get("like_count"),
        "tags": (metadata.get("tags") or [])[:10] or None,
        "categories": (metadata.get("categories") or [])[:5] or None,
        "media_type": metadata.get("media_type"),
        "format": metadata.get("format"),
        "source_quality": metadata.get("source_quality"),
        "actual_quality": metadata.get("actual_quality"),
        "video_codec": metadata.get("video_codec"),
        "audio_codec": metadata.get("audio_codec"),
        "width": metadata.get("width"),
        "height": metadata.get("height"),
        "size": metadata.get("size"),
        "available_qualities": metadata.get("available_qualities"),
    }
    summary = {key: value for key, value in summary.items() if value is not None}
    encoded = base64.urlsafe_b64encode(
        json.dumps(summary, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    headers = {
        "X-Media-Metadata": encoded,
        "X-Media-Metadata-Encoding": "base64url+json",
        "X-Media-Title": quote(str(metadata.get("title") or ""), safe=""),
        "X-Media-Duration": str(metadata.get("duration") or ""),
        "X-Media-Upload-Date": str(metadata.get("upload_date") or ""),
        "X-Media-Uploader": quote(str(metadata.get("uploader") or ""), safe=""),
        "X-Media-Source-Quality": str(metadata.get("source_quality") or ""),
        "X-Media-Actual-Quality": str(metadata.get("actual_quality") or ""),
        "X-Actual-Quality": str(metadata.get("actual_quality") or ""),
        "X-Media-Video-Codec": str(metadata.get("video_codec") or ""),
        "X-Media-Audio-Codec": str(metadata.get("audio_codec") or ""),
        "X-Media-Size": str(metadata.get("size") or ""),
        "X-Quality-Clamped": ("true" if metadata.get("quality_clamped") else "false")
        if "quality_clamped" in metadata
        else "",
    }
    return {key: value for key, value in headers.items() if value}


async def _enqueue_request_job(
    arq: Any,
    store: Store,
    job: dict[str, Any],
    job_key: str,
) -> bool:
    for _ in range(3):
        token = uuid.uuid4().hex
        arq_job_id = f"{job_key}:{token}"
        job.update(
            arq_token=token,
            arq_job_id=arq_job_id,
            last_enqueued_at=iso(utcnow()),
        )
        await store.save_job(job)
        try:
            result = await arq.enqueue_job(
                "process_request",
                job_key,
                token,
                _job_id=arq_job_id,
            )
        except Exception:
            LOGGER.exception("failed to enqueue job %s", job_key)
            return False
        if result is not None:
            return True
        await store.clear_arq_job(arq_job_id)
    return False


async def _accepted_response(
    store: Store,
    settings: Settings,
    job: dict[str, Any],
) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content=await _status_payload(store, settings, job),
        headers={"Retry-After": str(settings.retry_after_seconds)},
    )


async def _ensure_download_url(
    store: Store,
    settings: Settings,
    job: dict[str, Any],
) -> str:
    token = await store.get_or_create_download_token(
        str(job["request_key"]),
        settings.job_ttl,
    )
    if job.get("download_token") != token:
        job["download_token"] = token
        await store.save_job(job, ttl=settings.job_ttl)
    return f"/downloads/{token}"


async def _ready_response(
    store: Store,
    settings: Settings,
    job: dict[str, Any],
) -> JSONResponse:
    download_url = await _ensure_download_url(store, settings, job)
    payload = await _status_payload(store, settings, job)
    payload.update(
        {
            "status": "ready",
            "download_url": download_url,
            "filename": job.get("output_filename"),
            "content_type": job.get("content_type"),
            "size": job.get("size"),
        }
    )
    return JSONResponse(status_code=200, content=payload)


def create_app(
    settings: Settings | None = None,
    redis_client: aioredis.Redis | None = None,
    arq_pool: ArqRedis | None = None,
    metadata_db: MetadataDatabase | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    owned_redis = redis_client is None
    owned_pool = arq_pool is None
    owned_metadata_db = metadata_db is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app_settings.ensure_dirs()
        redis: Any = redis_client or aioredis.Redis.from_url(
            app_settings.redis_url,
            decode_responses=True,
        )
        pool: Any = arq_pool or await create_pool(RedisSettings.from_dsn(app_settings.redis_url))
        app.state.settings = app_settings
        app.state.redis = redis
        app.state.store = Store(redis, app_settings)
        app.state.domain_state = DomainState(redis, app_settings)
        app.state.arq = pool
        db = metadata_db or MetadataDatabase(app_settings.metadata_db_path)
        app.state.metadata_db = db
        if app_settings.auth_enabled and await redis.hlen("ml:auth:names") == 0:
            LOGGER.warning(
                "no API tokens configured; create one with media-loader-auth create --name ..."
            )
        yield
        if owned_metadata_db:
            await app.state.metadata_db.close()
        if owned_pool:
            await pool.aclose()
        if owned_redis:
            await redis.aclose()

    app = FastAPI(
        title="media-loader",
        version="0.1.0",
        docs_url="/docs" if app_settings.docs_enabled else None,
        redoc_url="/redoc" if app_settings.docs_enabled else None,
        openapi_url="/openapi.json" if app_settings.docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next: Any) -> Any:
        path = request.url.path
        public = (
            path in {"/", "/ui", "/ui/config", "/health"}
            or path.startswith("/static/")
            or path.startswith("/downloads/")
        )
        if public or not app_settings.auth_enabled:
            return await call_next(request)
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            record = await authenticate_async(
                request.app.state.redis,
                token.strip(),
                app_settings.auth_last_used_update_interval,
            )
        except RedisError:
            return JSONResponse(
                status_code=503,
                content={"detail": "Authentication backend unavailable"},
            )
        if not record:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or revoked token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        store: Store = request.app.state.store
        checks: dict[str, str] = {}
        try:
            await store.redis.ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "error"
        try:
            await request.app.state.metadata_db.ping()
            checks["sqlite"] = "ok"
        except Exception:
            checks["sqlite"] = "error"
        host, port = proxy_host_port(app_settings.proxy_url)
        try:
            async with asyncio.timeout(3):
                reader, writer = await asyncio.open_connection(host, port)
                del reader
                writer.close()
                await writer.wait_closed()
            checks["byedpi"] = "ok"
        except Exception:
            checks["byedpi"] = "error"
        healthy = all(value == "ok" for value in checks.values())
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ok" if healthy else "degraded", "checks": checks},
        )

    @app.get("/", include_in_schema=False)
    @app.get("/ui", include_in_schema=False)
    async def ui_index() -> FileResponse:
        index = STATIC_DIR / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=404, detail="UI is not installed")
        return FileResponse(index)

    @app.get("/ui/config", include_in_schema=False)
    async def ui_config() -> JSONResponse:
        qualities = [
            item.strip() for item in app_settings.quality_levels.split(",") if item.strip()
        ]
        return JSONResponse(
            content={
                "auth_required": app_settings.auth_enabled,
                "default_video_format": app_settings.default_video_format,
                "default_audio_format": app_settings.default_audio_format,
                "quality_levels": qualities,
                "video_formats": sorted(VIDEO_FORMATS),
                "audio_formats": sorted(AUDIO_FORMATS),
            }
        )

    @app.get("/downloads/{token}", include_in_schema=False)
    async def download(token: str, request: Request) -> FileResponse:
        store: Store = request.app.state.store
        job_key = await store.resolve_download_token(token)
        if not job_key:
            raise HTTPException(status_code=404, detail="download link expired")
        job = await store.get_job(job_key)
        if job is None:
            raise HTTPException(status_code=404, detail="download link expired")
        path = _job_output_path(job, store)
        if path is None:
            raise HTTPException(status_code=404, detail="file is no longer available")
        return FileResponse(
            path,
            media_type=str(job.get("content_type") or "application/octet-stream"),
            filename=str(job.get("output_filename") or path.name),
            headers={
                "Cache-Control": "private, no-store",
                "X-Cache": "DERIVED",
            },
        )

    @app.post("/videos")
    async def videos(payload: VideoRequest, request: Request) -> Any:
        try:
            resolved = resolve_request(app_settings, payload)
        except InvalidRequestError as exc:
            raise HTTPException(status_code=422, detail=exc.message) from exc

        try:
            canonical_url = canonicalize_url(str(payload.url))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        key = source_key(canonical_url)
        job_key = request_key(
            canonical_url,
            resolved.media_type,
            resolved.output_format,
            resolved.quality,
        )
        store: Store = request.app.state.store
        source = await store.get_source(key)

        if source and source.get("status") == "ready":
            source_path = Path(str(source.get("source_path") or ""))
            expires = parse_dt(str(source.get("expires_at") or ""))
            if not source_path.is_file() or expires is None or expires <= utcnow():
                await store.delete_source(key, force=True)
                source = None
            else:
                await store.mark_source_access(key)

        if source and source.get("status") == "ready":
            if resolved.media_type == "video" and not source.get("has_video"):
                raise HTTPException(status_code=422, detail="source has no video stream")
            if resolved.media_type == "audio" and not source.get("audio_codec"):
                raise HTTPException(status_code=422, detail="source has no audio stream")

        if payload.metadata_only:
            metadata: dict[str, Any] | None
            if source and source.get("status") == "ready":
                metadata = _source_metadata(source)
            else:
                try:
                    metadata = await request.app.state.metadata_db.get(key)
                except Exception:
                    LOGGER.exception("failed to read metadata from SQLite")
                    metadata = None
                if metadata is None:
                    try:
                        meta = await fetch_metadata_with_fallback(
                            canonical_url,
                            host_key(canonical_url),
                            app_settings,
                            request.app.state.domain_state,
                        )
                    except MediaLoaderError as exc:
                        raise HTTPException(
                            status_code=exc.http_status, detail=exc.message
                        ) from exc
                    except Exception as exc:
                        LOGGER.exception("metadata probe failed")
                        raise HTTPException(
                            status_code=502,
                            detail="metadata probe failed",
                        ) from exc
                    metadata = metadata_to_dict(meta)
                    try:
                        await request.app.state.metadata_db.upsert(
                            key,
                            canonical_url,
                            metadata,
                        )
                    except Exception:
                        LOGGER.exception("failed to save metadata to SQLite")
            if resolved.media_type == "video" and not metadata.get("has_video"):
                raise HTTPException(status_code=422, detail="source has no video stream")
            if resolved.media_type == "audio" and not metadata.get("has_audio"):
                raise HTTPException(status_code=422, detail="source has no audio stream")
            return JSONResponse(
                status_code=200,
                content=_metadata_payload(
                    resolved,
                    key,
                    metadata,
                    source,
                ),
            )
        if (
            not payload.respond_json
            and source
            and source.get("status") == "ready"
            and resolved.media_type == "video"
            and resolved.output_format == "mkv"
            and source.get("has_video")
        ):
            source_plan = plan_quality(
                resolved.quality,
                int(source["width"]) if source.get("width") else None,
                int(source["height"]) if source.get("height") else None,
            )
            if source_plan.source_quality is not None and not source_plan.needs_scale:
                source_path = Path(str(source["source_path"]))
                if not is_within(app_settings.cache_dir, source_path):
                    raise HTTPException(status_code=500, detail="invalid cache path")
                source_metadata = _metadata_payload(
                    resolved,
                    key,
                    _source_metadata(source),
                    source,
                )
                headers = _metadata_headers(source_metadata)
                headers["X-Source-Key"] = key
                headers["X-Quality-Clamped"] = (
                    "true" if source_metadata.get("quality_clamped") else "false"
                )
                return _file_response(
                    source_path,
                    "video/x-matroska",
                    str(source.get("source_filename") or source_path.name),
                    "HIT",
                    **headers,
                )

        existing = await store.get_job(job_key)
        if existing:
            if existing.get("status") == "ready":
                if payload.respond_json:
                    if _job_output_path(existing, store) is not None:
                        return await _ready_response(store, app_settings, existing)
                    await store.delete_job(job_key)
                    existing = None
                else:
                    served = await _serve_job(existing, store)
                    if served is not None:
                        return served
                    await store.delete_job(job_key)
                    existing = None
            elif existing.get("status") == "failed":
                finished = parse_dt(str(existing.get("finished_at") or ""))
                age = (utcnow() - finished).total_seconds() if finished else 10**9
                if age < app_settings.failed_retry_seconds:
                    return _failed_response(existing, app_settings)
                await store.delete_job(job_key)
                existing = None

        if existing:
            if existing.get("status") == "queued":
                last_enqueued = parse_dt(
                    str(existing.get("last_enqueued_at") or existing.get("created_at") or "")
                )
                age = (utcnow() - last_enqueued).total_seconds() if last_enqueued else 10**9
                if age >= app_settings.queued_reenqueue_seconds:
                    old_arq_id = str(existing.get("arq_job_id") or job_key)
                    await store.clear_arq_job(old_arq_id)
                    if not await _enqueue_request_job(
                        request.app.state.arq,
                        store,
                        existing,
                        job_key,
                    ):
                        LOGGER.error("failed to re-enqueue queued job %s", job_key)
            return await _accepted_response(store, app_settings, existing)

        job = _new_job_record(
            app_settings,
            resolved.media_type,
            resolved.output_format,
            resolved.quality,
            source,
            canonical_url,
            job_key,
            key,
        )
        created = await store.create_job_nx(job)
        if not created:
            existing = await store.get_job(job_key)
            if existing:
                if existing.get("status") == "ready":
                    if payload.respond_json:
                        if _job_output_path(existing, store) is not None:
                            return await _ready_response(store, app_settings, existing)
                    else:
                        served = await _serve_job(existing, store)
                        if served is not None:
                            return served
                    await store.delete_job(job_key)
                    existing = None
                if existing:
                    return await _accepted_response(store, app_settings, existing)
                created = await store.create_job_nx(job)
                if not created:
                    existing = await store.get_job(job_key)
                    if existing:
                        return await _accepted_response(store, app_settings, existing)
        if not await _enqueue_request_job(
            request.app.state.arq,
            store,
            job,
            job_key,
        ):
            job.update(
                status="failed",
                stage="failed",
                error="failed to enqueue job",
                error_code="enqueue_failed",
                http_status=503,
                finished_at=iso(utcnow()),
            )
            await store.save_job(job)
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "error": job["error"]},
                headers={"Retry-After": "10"},
            )

        return await _accepted_response(store, app_settings, job)

    return app


app = create_app()
