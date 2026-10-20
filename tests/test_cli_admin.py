from __future__ import annotations

import json

import fakeredis
import pytest
import redis

import media_loader.admin as admin
from media_loader.config import Settings
from media_loader.metadata_db import MetadataDatabase
from media_loader.store import SOURCE_SIZE_KEY, SOURCE_TTL_ZSET


def test_admin_cache_stats_and_proxy_reset(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    client.zadd(SOURCE_TTL_ZSET, {"source1": 9999999999})
    client.set(SOURCE_SIZE_KEY, 123)
    client.set("ml:domain:example.com", "{}")
    monkeypatch.setattr(admin, "get_settings", lambda: settings)
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: client)
    database = MetadataDatabase(settings.metadata_db_path)
    import asyncio

    asyncio.run(database.upsert("source1", "https://example.com", {"title": "x"}))
    asyncio.run(database.close())

    admin.main(["cache-stats"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["source_count"] == 1
    assert payload["source_size"] == 123
    assert payload["metadata_rows"] == 1
    assert payload["metadata_db_path"] == str(settings.metadata_db_path)

    admin.main(["proxy-reset", "example.com"])
    assert "reset example.com: ok" in capsys.readouterr().out
    assert not client.exists("ml:domain:example.com")
    client.close()
