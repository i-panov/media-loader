from __future__ import annotations

import asyncio
import base64
import json
from datetime import timedelta
from typing import Any, cast

import fakeredis.aioredis
import httpx
import pytest

from media_loader.api import create_app
from media_loader.auth import AUTH_NAMES_KEY, AUTH_TOKEN_PREFIX, hash_token
from media_loader.canonical import canonicalize_url, request_key, source_key
from media_loader.config import Settings
from media_loader.store import Store
from media_loader.utils import iso, utcnow
from media_loader.ytdlp import MediaMetadata


class FakeArq:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def enqueue_job(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((name, args, kwargs))
        return object()


def _source_record(settings: Settings, url: str, content: bytes = b"SOURCE") -> dict[str, Any]:
    canonical = canonicalize_url(url)
    key = source_key(canonical)
    directory = settings.cache_dir / key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "source.mkv"
    path.write_bytes(content)
    now = utcnow()
    return {
        "source_key": key,
        "url": url,
        "canonical_url": canonical,
        "status": "ready",
        "created_at": iso(now),
        "download_started_at": iso(now),
        "finished_at": iso(now),
        "expires_at": iso(now + timedelta(hours=1)),
        "source_path": str(path),
        "source_filename": "Example [1080p].mkv",
        "source_ext": "mkv",
        "source_size": len(content),
        "has_video": True,
        "width": 1920,
        "height": 1080,
        "source_quality": "1080p",
        "video_codec": "h264",
        "audio_codec": "aac",
        "duration": 10.0,
        "title": "Example",
        "thumbnail": None,
        "description": "Example description",
        "available_qualities": ["240p", "360p", "480p", "720p", "1080p"],
        "metadata_details": {
            "id": "abc123",
            "uploader": "Tester",
            "upload_date": "20260101",
            "tags": ["one", "two"],
        },
        "via_proxy": False,
        "attempts": 1,
        "error": None,
    }


async def _client(
    settings: Settings,
    redis: fakeredis.aioredis.FakeRedis,
) -> tuple[Any, httpx.AsyncClient, FakeArq]:
    arq = FakeArq()
    app = create_app(settings=settings, redis_client=redis, arq_pool=cast(Any, arq))
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return app, client, arq


async def test_source_cache_hit_returns_file(settings: Settings) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    url = "https://example.com/video"
    store = Store(redis, settings)
    record = _source_record(settings, url)
    await store.save_source(record)
    await store.mark_source_ready(record)
    app, client, _ = await _client(settings, redis)
    async with app.router.lifespan_context(app):
        response = await client.post("/videos", json={"url": url, "format": "mkv"})
        assert response.status_code == 200
        assert response.content == b"SOURCE"
        assert response.headers["x-cache"] == "HIT"
        assert response.headers["x-actual-quality"] == "1080p"
        assert response.headers["x-media-title"] == "Example"
        encoded = response.headers["x-media-metadata"]
        metadata = json.loads(base64.urlsafe_b64decode(encoded + "=="))
        assert metadata["title"] == "Example"
        assert metadata["uploader"] == "Tester"
        assert metadata["description"] == "Example description"
    await client.aclose()
    await redis.aclose()


async def test_metadata_only_from_cache_does_not_enqueue(settings: Settings) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    url = "https://example.com/video"
    store = Store(redis, settings)
    record = _source_record(settings, url)
    await store.save_source(record)
    await store.mark_source_ready(record)
    app, client, arq = await _client(settings, redis)
    async with app.router.lifespan_context(app):
        response = await client.post(
            "/videos",
            json={"url": url, "metadata_only": True},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["metadata_only"] is True
        assert body["source_cached"] is True
        assert body["title"] == "Example"
        assert body["description"] == "Example description"
        assert body["uploader"] == "Tester"
        assert body["available_qualities"] == [
            "240p",
            "360p",
            "480p",
            "720p",
            "1080p",
        ]
        assert body["size"] == len(b"SOURCE")
        assert arq.calls == []
    await client.aclose()
    await redis.aclose()


async def test_metadata_only_probe_does_not_create_job(
    settings: Settings,
    monkeypatch: Any,
) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    calls = 0

    async def fake_metadata(*args: Any, **kwargs: Any) -> MediaMetadata:
        nonlocal calls
        calls += 1
        return MediaMetadata(
            title="Remote title",
            description="Remote description",
            duration=42.0,
            thumbnail="https://example.com/thumb.jpg",
            has_video=True,
            has_audio=True,
            width=1280,
            height=720,
            max_quality="720p",
            available_qualities=("360p", "720p"),
            video_codec="h264",
            audio_codec="aac",
            details={"id": "remote", "uploader": "Remote user", "tags": ["x"]},
        )

    monkeypatch.setattr("media_loader.api.fetch_metadata_with_fallback", fake_metadata)
    app, client, arq = await _client(settings, redis)
    async with app.router.lifespan_context(app):
        response = await client.post(
            "/videos",
            json={
                "url": "https://example.com/video",
                "metadata_only": True,
                "format": "mp4",
                "quality": "360p",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["title"] == "Remote title"
        assert body["description"] == "Remote description"
        assert body["actual_quality"] == "360p"
        assert body["source_quality"] == "720p"
        assert body["source_cached"] is False
        assert arq.calls == []
        assert calls == 1

        again = await client.post(
            "/videos",
            json={"url": "https://example.com/video", "metadata_only": True},
        )
        assert again.status_code == 200
        assert calls == 1
    await client.aclose()
    await redis.aclose()


async def test_derived_format_creates_job_and_can_be_polled(settings: Settings) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    url = "https://example.com/video"
    store = Store(redis, settings)
    record = _source_record(settings, url)
    await store.save_source(record)
    await store.mark_source_ready(record)
    app, client, arq = await _client(settings, redis)
    async with app.router.lifespan_context(app):
        response = await client.post("/videos", json={"url": url, "audio_only": True})
        assert response.status_code == 202
        body = response.json()
        assert body["format"] == "mp3"
        assert body["source_cached"] is True
        assert len(arq.calls) == 1

        job_key = request_key(canonicalize_url(url), "audio", "mp3", None)
        job = await store.get_job(job_key)
        assert job is not None
        assert job["url"] == canonicalize_url(url)
        output_dir = settings.job_dir / job_key
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / "Example.mp3"
        output.write_bytes(b"MP3")
        job.update(
            status="ready",
            stage="ready",
            output_path=str(output),
            output_filename=output.name,
            content_type="audio/mpeg",
            size=3,
            actual_quality=None,
            finished_at=iso(utcnow()),
        )
        await store.save_job(job, ttl=60)

        response = await client.post("/videos", json={"url": url, "audio_only": True})
        assert response.status_code == 200
        assert response.content == b"MP3"
        assert response.headers["content-type"].startswith("audio/mpeg")
        encoded = response.headers["x-media-metadata"]
        metadata = json.loads(base64.urlsafe_b64decode(encoded + "=="))
        assert metadata["media_type"] == "audio"
        assert metadata["format"] == "mp3"
    await client.aclose()
    await redis.aclose()


async def test_lower_quality_creates_job(settings: Settings) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    url = "https://example.com/video"
    store = Store(redis, settings)
    record = _source_record(settings, url)
    await store.save_source(record)
    await store.mark_source_ready(record)
    app, client, arq = await _client(settings, redis)
    async with app.router.lifespan_context(app):
        response = await client.post(
            "/videos",
            json={"url": url, "format": "mkv", "quality": "720p"},
        )
        assert response.status_code == 202
        assert response.json()["requested_quality"] == "720p"
        assert len(arq.calls) == 1
    await client.aclose()
    await redis.aclose()


async def test_invalid_format_and_audio_quality(settings: Settings) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app, client, _ = await _client(settings, redis)
    async with app.router.lifespan_context(app):
        assert (
            await client.post(
                "/videos",
                json={"url": "https://example.com/video", "format": "webm"},
            )
        ).status_code == 422
        assert (
            await client.post(
                "/videos",
                json={
                    "url": "https://example.com/video",
                    "audio_only": True,
                    "quality": "720p",
                },
            )
        ).status_code == 422
    await client.aclose()
    await redis.aclose()


async def test_auth_required_and_health_open(settings: Settings) -> None:
    settings.auth_enabled = True
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app, client, _ = await _client(settings, redis)
    async with app.router.lifespan_context(app):
        unauth = await client.post("/videos", json={"url": "https://example.com/video"})
        assert unauth.status_code == 401
        assert unauth.headers["www-authenticate"] == "Bearer"

        token = "mlk_test_token"
        digest = hash_token(token)
        await redis.hset(AUTH_NAMES_KEY, "phone", digest)
        await redis.hset(
            f"{AUTH_TOKEN_PREFIX}{digest}",
            mapping={
                "name": "phone",
                "token_id": digest[:12],
                "created_at": iso(utcnow()),
                "last_used_at": "",
                "expires_at": "",
            },
        )
        auth = await client.post(
            "/videos",
            json={"url": "https://example.com/video"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert auth.status_code == 202

        health = await client.get("/health")
        assert health.status_code != 401
    await client.aclose()
    await redis.aclose()


async def test_failed_job_returns_non_200(settings: Settings) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    url = "https://example.com/video"
    store = Store(redis, settings)
    app, client, _ = await _client(settings, redis)
    async with app.router.lifespan_context(app):
        response = await client.post("/videos", json={"url": url, "format": "mkv"})
        assert response.status_code == 202
        job_key = request_key(canonicalize_url(url), "video", "mkv", None)
        job = await store.get_job(job_key)
        assert job is not None
        job.update(
            status="failed",
            http_status=502,
            error="boom",
            error_code="download_failed",
            finished_at=iso(utcnow() - timedelta(seconds=60)),
        )
        await store.save_job(job)
        response = await client.post("/videos", json={"url": url, "format": "mkv"})
        assert response.status_code != 200
    await client.aclose()
    await redis.aclose()


async def test_health_endpoint(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app, client, _ = await _client(settings, redis)

    class Writer:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def fake_connection(*args: Any, **kwargs: Any) -> tuple[Any, Writer]:
        del args, kwargs
        return object(), Writer()

    monkeypatch.setattr(asyncio, "open_connection", fake_connection)
    async with app.router.lifespan_context(app):
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["checks"]["byedpi"] == "ok"
    await client.aclose()
    await redis.aclose()


async def test_polling_and_failed_response(settings: Settings) -> None:
    settings.failed_retry_seconds = 60
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    app, client, _ = await _client(settings, redis)
    async with app.router.lifespan_context(app):
        first = await client.post("/videos", json={"url": "https://example.com/video"})
        assert first.status_code == 202
        second = await client.post("/videos", json={"url": "https://example.com/video"})
        assert second.status_code == 202
        assert second.json()["status"] == "queued"

        job_key = request_key(canonicalize_url("https://example.com/video"), "video", "mp4", None)
        job = await store.get_job(job_key)
        assert job is not None
        job.update(
            status="failed",
            http_status=502,
            error="boom",
            error_code="download_failed",
            finished_at=iso(utcnow()),
        )
        await store.save_job(job)
        failed = await client.post("/videos", json={"url": "https://example.com/video"})
        assert failed.status_code == 502
        assert failed.json()["error"] == "boom"
    await client.aclose()
    await redis.aclose()


async def test_ready_job_with_missing_file_is_recreated(settings: Settings) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    app, client, _ = await _client(settings, redis)
    async with app.router.lifespan_context(app):
        first = await client.post("/videos", json={"url": "https://example.com/video"})
        assert first.status_code == 202
        job_key = request_key(canonicalize_url("https://example.com/video"), "video", "mp4", None)
        job = await store.get_job(job_key)
        assert job is not None
        job.update(
            status="ready",
            output_path=str(settings.job_dir / job_key / "missing.mp4"),
            output_filename="missing.mp4",
            content_type="video/mp4",
            finished_at=iso(utcnow()),
        )
        await store.save_job(job)
        second = await client.post("/videos", json={"url": "https://example.com/video"})
        assert second.status_code == 202
    await client.aclose()
    await redis.aclose()


async def test_invalid_token(settings: Settings) -> None:
    settings.auth_enabled = True
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app, client, _ = await _client(settings, redis)
    async with app.router.lifespan_context(app):
        response = await client.post(
            "/videos",
            json={"url": "https://example.com/video"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert response.status_code == 401
    await client.aclose()
    await redis.aclose()


async def test_orphan_queued_job_is_reenqueued(settings: Settings) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    app, client, arq = await _client(settings, redis)
    async with app.router.lifespan_context(app):
        first = await client.post("/videos", json={"url": "https://example.com/video"})
        assert first.status_code == 202
        job_key = request_key(canonicalize_url("https://example.com/video"), "video", "mp4", None)
        job = await store.get_job(job_key)
        assert job is not None
        job["last_enqueued_at"] = iso(utcnow() - timedelta(seconds=60))
        await store.save_job(job)
        calls_before = len(arq.calls)
        second = await client.post("/videos", json={"url": "https://example.com/video"})
        assert second.status_code == 202
        assert len(arq.calls) == calls_before + 1
        refreshed = await store.get_job(job_key)
        assert refreshed is not None
        assert refreshed["arq_job_id"] == f"{job_key}:{refreshed['arq_token']}"
        assert arq.calls[-1][1][1] == refreshed["arq_token"]
    await client.aclose()
    await redis.aclose()
