from __future__ import annotations

import fakeredis.aioredis

from media_loader.config import Settings
from media_loader.domain_state import DomainState


async def test_domain_state_roundtrip(settings: Settings) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    state = DomainState(redis, settings)
    assert await state.get("youtube.com") is None
    assert await state.get_mode("youtube.com") is None
    await state.set("youtube.com", "proxy", "blocked")
    record = await state.get("youtube.com")
    assert record is not None
    assert record["mode"] == "proxy"
    assert await state.get_mode("youtube.com") == "proxy"
    items = await state.list()
    assert len(items) == 1
    assert items[0]["host"] == "youtube.com"
    await state.delete("youtube.com")
    assert await state.get_mode("youtube.com") is None
    await redis.aclose()
