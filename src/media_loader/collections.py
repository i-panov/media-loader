from __future__ import annotations

import hashlib
import json
from typing import Any, cast

import redis.asyncio as aioredis

from media_loader.config import Settings
from media_loader.schemas import MAX_FORMAT
from media_loader.series import CollectionSpec, spec_from_dict, spec_to_dict
from media_loader.store import Store
from media_loader.utils import iso, utcnow

COLLECTION_PREFIX = "ml:collection:"
SERIES_CACHE_PREFIX = "ml:series:"


def collection_key(
    series_id: str,
    scope: str,
    season: int | None,
    media_type: str,
    output_format: str,
    quality: str | None,
    episodes: tuple[int, ...] | None = None,
) -> str:
    material = (
        f"{series_id}|{scope}|{season if season is not None else ''}|"
        f"{media_type}:{output_format}|{quality or MAX_FORMAT}"
    )
    if episodes:
        material += "|episodes:" + ",".join(str(number) for number in sorted(set(episodes)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def series_cache_key(url: str, scope: str) -> str:
    material = f"{url}|{scope}".encode()
    return f"{SERIES_CACHE_PREFIX}{hashlib.sha256(material).hexdigest()[:32]}"


class SeriesCache:
    def __init__(self, redis: aioredis.Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings

    async def get(self, url: str, scope: str) -> CollectionSpec | None:
        raw = await self.redis.get(series_cache_key(url, scope))
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        return spec_from_dict(cast(dict[str, Any], value))

    async def set(self, url: str, scope: str, spec: CollectionSpec) -> None:
        await self.redis.set(
            series_cache_key(url, scope),
            json.dumps(spec_to_dict(spec), ensure_ascii=False),
            ex=max(60, self.settings.series_cache_ttl),
        )


class CollectionStore:
    def __init__(self, redis: aioredis.Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings

    def _key(self, key: str) -> str:
        return f"{COLLECTION_PREFIX}{key}"

    def _default_ttl(self, status: str | None) -> int:
        if status in {"ready", "failed"}:
            return self.settings.collection_ttl + 300
        return self.settings.collection_job_timeout + self.settings.collection_ttl + 300

    async def get(self, key: str) -> dict[str, Any] | None:
        raw = await self.redis.get(self._key(key))
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        return cast(dict[str, Any], value) if isinstance(value, dict) else None

    async def save(self, record: dict[str, Any], ttl: int | None = None) -> None:
        status = str(record.get("status") or "queued")
        await self.redis.set(
            self._key(str(record["collection_key"])),
            json.dumps(record, ensure_ascii=False),
            ex=max(60, int(ttl or self._default_ttl(status))),
        )

    async def create_nx(self, record: dict[str, Any]) -> bool:
        created = await self.redis.set(
            self._key(str(record["collection_key"])),
            json.dumps(record, ensure_ascii=False),
            nx=True,
            ex=self._default_ttl("queued"),
        )
        return bool(created)

    async def delete(self, key: str) -> None:
        await self.redis.delete(self._key(key))

    async def recover_incomplete(self, store: Store) -> int:
        recovered = 0
        async for raw_key in self.redis.scan_iter(
            match=f"{COLLECTION_PREFIX}*",
            count=100,
        ):
            key = str(raw_key)[len(COLLECTION_PREFIX) :]
            record = await self.get(key)
            if not record or record.get("status") not in {"queued", "processing"}:
                continue
            record.update(
                status="failed",
                stage="failed",
                error="worker restarted before collection finished",
                error_code="worker_restarted",
                finished_at=iso(utcnow()),
            )
            record["progress"] = {"percent": None, "stage": "failed"}
            record.pop("arq_token", None)
            await self.save(record)
            arq_job_id = str(record.get("arq_job_id") or key)
            await store.clear_arq_job(arq_job_id)
            recovered += 1
        return recovered
