from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Literal

import redis.asyncio as aioredis

from media_loader.config import Settings
from media_loader.utils import iso, utcnow

DOMAIN_PREFIX = "ml:domain:"
DomainMode = Literal["direct", "proxy"]


def _key(host: str) -> str:
    return f"{DOMAIN_PREFIX}{host}"


class DomainState:
    def __init__(self, redis: aioredis.Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings

    async def get(self, host: str) -> dict[str, Any] | None:
        raw = await self.redis.get(_key(host))
        return json.loads(raw) if raw else None

    async def get_mode(self, host: str) -> DomainMode | None:
        record = await self.get(host)
        mode = record.get("mode") if record else None
        return mode if mode in {"direct", "proxy"} else None

    async def set(self, host: str, mode: DomainMode, reason: str) -> None:
        now = utcnow()
        record = {
            "host": host,
            "mode": mode,
            "reason": reason[:500],
            "checked_at": iso(now),
            "expires_at": iso(now + timedelta(seconds=self.settings.proxy_state_ttl)),
        }
        await self.redis.set(
            _key(host),
            json.dumps(record, ensure_ascii=False),
            ex=self.settings.proxy_state_ttl,
        )

    async def delete(self, host: str) -> None:
        await self.redis.delete(_key(host))

    async def list(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        keys = [
            str(key) async for key in self.redis.scan_iter(match=f"{DOMAIN_PREFIX}*", count=100)
        ]
        for key in sorted(keys):
            raw = await self.redis.get(key)
            if raw:
                result.append(json.loads(raw))
        return result
