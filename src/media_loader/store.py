from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from arq import constants as arq_constants

from media_loader.config import Settings
from media_loader.utils import iso, parse_dt, utcnow

SOURCE_PREFIX = "ml:source:"
SOURCE_TTL_ZSET = "ml:source:ttl"
SOURCE_LRU_ZSET = "ml:source:lru"
SOURCE_SIZE_KEY = "ml:source:size"
SOURCE_ACTIVE_PREFIX = "ml:source:active:"
SOURCE_PROGRESS_PREFIX = "ml:source:progress:"
JOB_PREFIX = "ml:job:"
JOB_PROGRESS_PREFIX = "ml:job:progress:"
DOWNLOAD_PREFIX = "ml:download:"
DOWNLOAD_JOB_PREFIX = "ml:download-job:"
LOCK_PREFIX = "ml:lock:"

_RELEASE_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_RENEW_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""


def _source_key(key: str) -> str:
    return f"{SOURCE_PREFIX}{key}"


def _active_key(key: str) -> str:
    return f"{SOURCE_ACTIVE_PREFIX}{key}"


def _job_key(key: str) -> str:
    return f"{JOB_PREFIX}{key}"


def source_progress_key(key: str) -> str:
    return f"{SOURCE_PROGRESS_PREFIX}{key}"


def job_progress_key(key: str) -> str:
    return f"{JOB_PROGRESS_PREFIX}{key}"


class Store:
    def __init__(self, redis: Any, settings: Settings) -> None:
        self.redis: Any = redis
        self.settings = settings

    def source_dir(self, key: str) -> Path:
        return self.settings.cache_dir / key

    def job_dir(self, key: str) -> Path:
        return self.settings.job_dir / key

    def lock_key(self, name: str) -> str:
        return f"{LOCK_PREFIX}{name}"

    def source_lock_name(self, source_key: str) -> str:
        return f"source:{source_key}"

    async def get_source(self, key: str) -> dict[str, Any] | None:
        raw = await self.redis.get(_source_key(key))
        return json.loads(raw) if raw else None

    async def save_source(self, record: dict[str, Any], ttl: int | None = None) -> None:
        status = record.get("status")
        if ttl is None:
            if status == "ready":
                ttl = self.settings.cache_ttl + self.settings.cache_cleanup_interval * 2
            elif status == "failed":
                ttl = self.settings.failed_record_ttl
            else:
                ttl = self.settings.job_timeout + self.settings.job_ttl
        await self.redis.set(
            _source_key(str(record["source_key"])),
            json.dumps(record, ensure_ascii=False),
            ex=max(60, int(ttl)),
        )

    async def mark_source_ready(self, record: dict[str, Any]) -> None:
        key = str(record["source_key"])
        expires = parse_dt(str(record.get("expires_at") or ""))
        if expires is None:
            return
        added = await self.redis.zadd(
            SOURCE_TTL_ZSET,
            {key: expires.timestamp()},
            nx=True,
        )
        now = time.time()
        await self.redis.zadd(SOURCE_LRU_ZSET, {key: now})
        if added:
            size = int(record.get("source_size") or 0)
            if size:
                await self.redis.incrby(SOURCE_SIZE_KEY, size)

    async def mark_source_access(self, key: str) -> None:
        await self.redis.zadd(SOURCE_LRU_ZSET, {key: time.time()})

    async def get_active(self, key: str) -> int:
        value = await self.redis.get(_active_key(key))
        return int(value or 0)

    async def incr_active(self, key: str) -> None:
        active_key = _active_key(key)
        await self.redis.incr(active_key)
        await self.redis.expire(
            active_key,
            self.settings.job_timeout + self.settings.job_ttl,
        )

    async def decr_active(self, key: str) -> None:
        active_key = _active_key(key)
        value = await self.redis.decr(active_key)
        if value <= 0:
            await self.redis.delete(active_key)

    async def delete_source(self, key: str, force: bool = False) -> bool:
        if not force and await self.get_active(key) > 0:
            return False
        record = await self.get_source(key)
        size = int(record.get("source_size") or 0) if record else 0
        await asyncio.to_thread(shutil.rmtree, self.source_dir(key), True)
        await self.redis.delete(
            _source_key(key),
            source_progress_key(key),
            _active_key(key),
        )
        await self.redis.zrem(SOURCE_TTL_ZSET, key)
        await self.redis.zrem(SOURCE_LRU_ZSET, key)
        if size:
            remaining = await self.redis.decrby(SOURCE_SIZE_KEY, size)
            if remaining < 0:
                await self.redis.set(SOURCE_SIZE_KEY, 0)
        return True

    async def cleanup_sources(self) -> dict[str, int]:
        now = time.time()
        removed = 0
        expired = await self.redis.zrangebyscore(SOURCE_TTL_ZSET, "-inf", now)
        for key in expired:
            if await self.delete_source(str(key)):
                removed += 1

        size = int(await self.redis.get(SOURCE_SIZE_KEY) or 0)
        while size > self.settings.cache_max_size:
            candidates = await self.redis.zrange(SOURCE_LRU_ZSET, 0, 19)
            if not candidates:
                break
            deleted = False
            for candidate in candidates:
                key = str(candidate)
                record = await self.get_source(key)
                old_size = int(record.get("source_size") or 0) if record else 0
                if await self.delete_source(key):
                    size -= old_size
                    removed += 1
                    deleted = True
                    break
            if not deleted:
                break

        orphaned = 0
        cache_dir = self.settings.cache_dir
        if cache_dir.exists():
            for path in await asyncio.to_thread(lambda: list(cache_dir.iterdir())):
                if not path.is_dir():
                    continue
                if not await self.redis.exists(_source_key(path.name)):
                    await asyncio.to_thread(shutil.rmtree, path, True)
                    await self.redis.zrem(SOURCE_TTL_ZSET, path.name)
                    await self.redis.zrem(SOURCE_LRU_ZSET, path.name)
                    orphaned += 1
        return {
            "removed": removed,
            "orphaned": orphaned,
            "size": int(await self.redis.get(SOURCE_SIZE_KEY) or 0),
        }

    async def cache_stats(self) -> dict[str, int]:
        return {
            "count": int(await self.redis.zcard(SOURCE_TTL_ZSET)),
            "size": int(await self.redis.get(SOURCE_SIZE_KEY) or 0),
            "limit": self.settings.cache_max_size,
            "expired": int(await self.redis.zcount(SOURCE_TTL_ZSET, "-inf", time.time())),
        }

    async def get_or_create_download_token(self, job_key: str, ttl: int) -> str:
        ttl = max(60, int(ttl))
        pointer = f"{DOWNLOAD_JOB_PREFIX}{job_key}"
        existing = await self.redis.get(pointer)
        if existing:
            token = str(existing)
            await self.redis.set(
                f"{DOWNLOAD_PREFIX}{token}",
                job_key,
                nx=True,
                ex=ttl,
            )
            return token
        token = secrets.token_urlsafe(32)
        created = await self.redis.set(pointer, token, nx=True, ex=ttl)
        if created:
            await self.redis.set(f"{DOWNLOAD_PREFIX}{token}", job_key, ex=ttl)
            return token
        winner = await self.redis.get(pointer)
        if winner:
            winner_token = str(winner)
            await self.redis.set(
                f"{DOWNLOAD_PREFIX}{winner_token}",
                job_key,
                nx=True,
                ex=ttl,
            )
            return winner_token
        return await self.get_or_create_download_token(job_key, ttl)

    async def save_download_token(self, token: str, job_key: str, ttl: int) -> None:
        await self.redis.set(f"{DOWNLOAD_PREFIX}{token}", job_key, ex=max(60, int(ttl)))
        await self.redis.set(
            f"{DOWNLOAD_JOB_PREFIX}{job_key}",
            token,
            nx=True,
            ex=max(60, int(ttl)),
        )

    async def resolve_download_token(self, token: str) -> str | None:
        value = await self.redis.get(f"{DOWNLOAD_PREFIX}{token}")
        return str(value) if value else None

    async def delete_download_token(self, token: str) -> None:
        await self.redis.delete(f"{DOWNLOAD_PREFIX}{token}")

    async def delete_download_token_for_job(self, job_key: str) -> None:
        pointer = f"{DOWNLOAD_JOB_PREFIX}{job_key}"
        token = await self.redis.get(pointer)
        await self.redis.delete(pointer)
        if token:
            await self.redis.delete(f"{DOWNLOAD_PREFIX}{token!s}")

    async def get_job(self, key: str) -> dict[str, Any] | None:
        raw = await self.redis.get(_job_key(key))
        return json.loads(raw) if raw else None

    async def save_job(self, record: dict[str, Any], ttl: int | None = None) -> None:
        if ttl is None:
            status = record.get("status")
            if status == "ready":
                ttl = self.settings.job_ttl
            elif status == "failed":
                ttl = self.settings.failed_record_ttl
            else:
                ttl = self.settings.job_timeout + self.settings.job_ttl
        await self.redis.set(
            _job_key(str(record["request_key"])),
            json.dumps(record, ensure_ascii=False),
            ex=max(60, int(ttl)),
        )

    async def create_job_nx(self, record: dict[str, Any]) -> bool:
        ttl = self.settings.job_timeout + self.settings.job_ttl
        created = await self.redis.set(
            _job_key(str(record["request_key"])),
            json.dumps(record, ensure_ascii=False),
            nx=True,
            ex=ttl,
        )
        return bool(created)

    async def delete_job(self, key: str) -> None:
        record = await self.get_job(key)
        await self.redis.delete(_job_key(key), job_progress_key(key))
        await asyncio.to_thread(shutil.rmtree, self.job_dir(key), True)
        if record:
            await self.clear_arq_job(str(record.get("arq_job_id") or key))
            await self.delete_download_token_for_job(key)
            if record.get("download_token"):
                await self.delete_download_token(str(record["download_token"]))

    async def cleanup_jobs(self) -> int:
        removed = 0
        job_dir = self.settings.job_dir
        if job_dir.exists():
            for path in await asyncio.to_thread(lambda: list(job_dir.iterdir())):
                if not path.is_dir():
                    continue
                record = await self.get_job(path.name)
                now = utcnow()
                if record is None:
                    await asyncio.to_thread(shutil.rmtree, path, True)
                    removed += 1
                    continue
                status = record.get("status")
                created = parse_dt(str(record.get("created_at") or ""))
                finished = parse_dt(str(record.get("finished_at") or ""))
                if status in {"ready", "failed"} and finished:
                    if (now - finished).total_seconds() > self.settings.job_ttl:
                        await self.delete_job(path.name)
                        removed += 1
                elif created and (now - created).total_seconds() > self.settings.job_timeout:
                    await self.delete_job(path.name)
                    removed += 1

        tmp_dir = self.settings.tmp_dir
        if tmp_dir.exists():
            cutoff = time.time() - self.settings.job_timeout
            for path in await asyncio.to_thread(lambda: list(tmp_dir.iterdir())):
                try:
                    mtime = path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if mtime < cutoff:
                    if path.is_dir():
                        await asyncio.to_thread(shutil.rmtree, path, True)
                    else:
                        await asyncio.to_thread(path.unlink, True)
        return removed

    async def get_progress(self, key: str) -> dict[str, Any]:
        raw = await self.redis.hgetall(key)
        result: dict[str, Any] = {}
        for field, value in raw.items():
            try:
                result[str(field)] = float(value)
            except (TypeError, ValueError):
                result[str(field)] = value
        return result

    async def acquire_lock(self, name: str, ttl: int) -> str | None:
        token = uuid.uuid4().hex
        acquired = await self.redis.set(self.lock_key(name), token, nx=True, ex=ttl)
        return token if acquired else None

    async def release_lock(self, name: str, token: str) -> None:
        await self.redis.eval(_RELEASE_LOCK_LUA, 1, self.lock_key(name), token)

    async def renew_lock(self, name: str, token: str, ttl: int) -> bool:
        renewed = await self.redis.eval(
            _RENEW_LOCK_LUA,
            1,
            self.lock_key(name),
            token,
            ttl,
        )
        return bool(renewed)

    async def owns_lock(self, name: str, token: str) -> bool:
        stored = await self.redis.get(self.lock_key(name))
        return bool(stored == token)

    async def clear_arq_job(self, job_id: str) -> None:
        await self.redis.delete(
            f"{arq_constants.job_key_prefix}{job_id}",
            f"{arq_constants.result_key_prefix}{job_id}",
            f"{arq_constants.in_progress_key_prefix}{job_id}",
            f"{arq_constants.retry_key_prefix}{job_id}",
        )
        await self.redis.zrem("arq:queue", job_id)

    async def recover_incomplete_jobs(self) -> int:
        recovered = 0
        async for raw_key in self.redis.scan_iter(match=f"{JOB_PREFIX}*", count=100):
            key = str(raw_key)
            if key.startswith(JOB_PROGRESS_PREFIX):
                continue
            request_key = key[len(JOB_PREFIX) :]
            record = await self.get_job(request_key)
            if not record or record.get("status") not in {
                "processing",
                "downloading_source",
                "waiting_for_source",
            }:
                continue
            record.update(
                status="failed",
                stage="failed",
                error="worker restarted before completion",
                error_code="worker_restarted",
                http_status=502,
                finished_at=iso(utcnow()),
            )
            record["progress"] = {"percent": None, "stage": "failed"}
            await self.save_job(record)
            await self.clear_arq_job(str(record.get("arq_job_id") or request_key))
            await asyncio.to_thread(shutil.rmtree, self.job_dir(request_key), True)
            if record.get("source_key"):
                await self.decr_active(str(record["source_key"]))
            recovered += 1
        return recovered
