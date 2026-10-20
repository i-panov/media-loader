from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import fakeredis.aioredis
import pytest

from media_loader.config import Settings
from media_loader.store import (
    SOURCE_LRU_ZSET,
    Store,
    job_progress_key,
)
from media_loader.utils import iso, utcnow


def _source_record(key: str, path: Path, size: int, *, expires_in: int = 3600) -> dict[str, Any]:
    now = utcnow()
    return {
        "source_key": key,
        "url": f"https://example.com/{key}",
        "canonical_url": f"https://example.com/{key}",
        "status": "ready",
        "created_at": iso(now),
        "download_started_at": iso(now),
        "finished_at": iso(now),
        "expires_at": iso(now + timedelta(seconds=expires_in)),
        "source_path": str(path),
        "source_filename": path.name,
        "source_ext": path.suffix.lstrip("."),
        "source_size": size,
        "has_video": True,
        "width": 1920,
        "height": 1080,
        "source_quality": "1080p",
        "video_codec": "h264",
        "audio_codec": "aac",
        "duration": 10.0,
        "title": "Example",
        "thumbnail": None,
        "via_proxy": False,
        "attempts": 1,
        "error": None,
    }


async def _create_source(
    store: Store,
    settings: Settings,
    key: str,
    size: int = 6,
) -> dict[str, Any]:
    directory = store.source_dir(key)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "source.mkv"
    path.write_bytes(b"x" * size)
    record = _source_record(key, path, size)
    await store.save_source(record)
    await store.mark_source_ready(record)
    return record


async def test_source_lifecycle_and_stats(settings: Settings) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    record = await _create_source(store, settings, "abc", 6)
    stored = await store.get_source("abc")
    assert stored is not None
    assert stored["title"] == "Example"
    stats = await store.cache_stats()
    assert stats["count"] == 1
    assert stats["size"] == 6
    await store.mark_source_access("abc")
    assert await store.delete_source("abc") is True
    assert not Path(record["source_path"]).exists()
    assert (await store.get_source("abc")) is None
    assert (await store.cache_stats())["count"] == 0
    await redis.aclose()


async def test_expired_and_lru_eviction(settings: Settings) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    settings.cache_max_size = 10
    await _create_source(store, settings, "old", 6)
    await _create_source(store, settings, "new", 6)
    await redis.zadd(SOURCE_LRU_ZSET, {"old": 1, "new": 2})
    result = await store.cleanup_sources()
    assert result["removed"] >= 1
    assert await store.get_source("old") is None
    assert await store.get_source("new") is not None
    await redis.aclose()


async def test_active_source_is_not_evicted(settings: Settings) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    settings.cache_max_size = 1
    await _create_source(store, settings, "busy", 10)
    await store.incr_active("busy")
    await store.cleanup_sources()
    assert await store.get_source("busy") is not None
    await store.decr_active("busy")
    await store.cleanup_sources()
    assert await store.get_source("busy") is None
    await redis.aclose()


async def test_job_nx_and_progress(settings: Settings) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    record = {"request_key": "job1", "status": "queued", "created_at": iso(utcnow())}
    assert await store.create_job_nx(record) is True
    assert await store.create_job_nx(record) is False
    stored_job = await store.get_job("job1")
    assert stored_job is not None
    assert stored_job["status"] == "queued"
    await redis.hset(
        job_progress_key("job1"),
        mapping={
            "percent": "50",
            "stage": "transcoding",
            "downloaded_bytes": "123",
        },
    )
    progress = await store.get_progress(job_progress_key("job1"))
    assert progress["percent"] == 50.0
    assert progress["stage"] == "transcoding"
    await store.delete_job("job1")
    assert await store.get_job("job1") is None
    await redis.aclose()


async def test_locks_and_cleanup_jobs(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)

    async def fake_eval(
        script: str,
        numkeys: int,
        key: str,
        token: str,
        *args: Any,
    ) -> int:
        del numkeys
        if await redis.get(key) != token:
            return 0
        if "expire" in script:
            await redis.expire(key, int(args[0]))
            return 1
        await redis.delete(key)
        return 1

    monkeypatch.setattr(redis, "eval", fake_eval)
    token = await store.acquire_lock("lock1", 60)
    assert token is not None
    assert await store.acquire_lock("lock1", 60) is None
    assert await store.owns_lock("lock1", token) is True
    assert await store.owns_lock("lock1", "wrong") is False
    assert await store.renew_lock("lock1", token, 10) is True
    await store.release_lock("lock1", token)
    token2 = await store.acquire_lock("lock1", 60)
    assert token2 is not None
    await store.release_lock("lock1", token2)

    old = iso(utcnow() - timedelta(hours=3))
    ready_dir = store.job_dir("ready")
    ready_dir.mkdir(parents=True)
    (ready_dir / "out.mp4").write_text("x")
    await store.save_job(
        {
            "request_key": "ready",
            "status": "ready",
            "created_at": old,
            "finished_at": old,
        }
    )
    queued_dir = store.job_dir("queued")
    queued_dir.mkdir(parents=True)
    await store.save_job(
        {
            "request_key": "queued",
            "status": "processing",
            "created_at": old,
            "finished_at": None,
        }
    )
    orphan_dir = store.job_dir("orphan")
    orphan_dir.mkdir(parents=True)
    tmp_old = settings.tmp_dir / "old-temp"
    tmp_old.mkdir(parents=True)
    old_timestamp = (utcnow() - timedelta(hours=4)).timestamp()
    import os

    os.utime(tmp_old, (old_timestamp, old_timestamp))

    removed = await store.cleanup_jobs()
    assert removed >= 3
    assert not ready_dir.exists()
    assert not queued_dir.exists()
    assert not orphan_dir.exists()
    assert not tmp_old.exists()
    assert await store.get_job("ready") is None
    await redis.aclose()


async def test_recover_incomplete_jobs(settings: Settings) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    for request_key, status in (
        ("processing", "processing"),
        ("downloading", "downloading_source"),
        ("waiting", "waiting_for_source"),
        ("queued", "queued"),
        ("ready", "ready"),
    ):
        directory = store.job_dir(request_key)
        directory.mkdir(parents=True)
        (directory / "artifact").write_text("x")
        await store.save_job(
            {
                "request_key": request_key,
                "status": status,
                "created_at": iso(utcnow()),
                "finished_at": None,
            }
        )
    await redis.hset(job_progress_key("processing"), mapping={"percent": "10"})
    for key in (
        "arq:job:processing",
        "arq:result:processing",
        "arq:in-progress:processing",
        "arq:retry:processing",
    ):
        await redis.set(key, "x")
    await redis.zadd("arq:queue", {"processing": 1})
    recovered = await store.recover_incomplete_jobs()
    assert recovered == 3
    for request_key in ("processing", "downloading", "waiting"):
        record = await store.get_job(request_key)
        assert record is not None
        assert record["status"] == "failed"
        assert record["error_code"] == "worker_restarted"
        assert not store.job_dir(request_key).exists()
    assert not await redis.exists("arq:job:processing")
    assert await redis.zscore("arq:queue", "processing") is None
    queued = await store.get_job("queued")
    ready = await store.get_job("ready")
    assert queued is not None and queued["status"] == "queued"
    assert ready is not None and ready["status"] == "ready"
    await redis.aclose()


async def test_clear_arq_job_artifacts(settings: Settings) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    job_id = "job123"
    for key in (
        f"arq:job:{job_id}",
        f"arq:result:{job_id}",
        f"arq:in-progress:{job_id}",
        f"arq:retry:{job_id}",
    ):
        await redis.set(key, "x")
    await redis.zadd("arq:queue", {job_id: 1})
    await store.clear_arq_job(job_id)
    for key in (
        f"arq:job:{job_id}",
        f"arq:result:{job_id}",
        f"arq:in-progress:{job_id}",
        f"arq:retry:{job_id}",
    ):
        assert not await redis.exists(key)
    assert await redis.zscore("arq:queue", job_id) is None
    await redis.aclose()
