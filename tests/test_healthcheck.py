from __future__ import annotations

import socket

import pytest
import redis

import media_loader.healthcheck as healthcheck
from media_loader.config import Settings
from media_loader.metadata_db import MetadataDatabase


class _Connection:
    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_healthcheck_success(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import asyncio

    database = MetadataDatabase(settings.metadata_db_path)
    asyncio.run(database.close())

    class Client:
        def ping(self) -> bool:
            return True

        def close(self) -> None:
            return None

    monkeypatch.setattr(healthcheck, "get_settings", lambda: settings)
    monkeypatch.setattr(
        redis.Redis,
        "from_url",
        lambda *args, **kwargs: Client(),
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: _Connection(),
    )
    healthcheck.main()
    assert "ok" in capsys.readouterr().out


def test_healthcheck_failure(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import asyncio

    database = MetadataDatabase(settings.metadata_db_path)
    asyncio.run(database.close())

    class Client:
        def ping(self) -> bool:
            raise RuntimeError("redis down")

        def close(self) -> None:
            return None

    monkeypatch.setattr(healthcheck, "get_settings", lambda: settings)
    monkeypatch.setattr(
        redis.Redis,
        "from_url",
        lambda *args, **kwargs: Client(),
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no proxy")),
    )
    with pytest.raises(SystemExit):
        healthcheck.main()
    error = capsys.readouterr().err
    assert "redis" in error
    assert "byedpi" in error
