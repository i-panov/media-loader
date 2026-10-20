from __future__ import annotations

import fakeredis
import pytest
import redis

import media_loader.auth as auth
from media_loader.config import Settings


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    client: fakeredis.FakeRedis,
) -> None:
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: client)


def test_cli_create_list_revoke(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    _patch(monkeypatch, settings, client)
    auth.main(["create", "--name", "phone", "--quiet"])
    token = capsys.readouterr().out.strip()
    assert token.startswith("mlk_")
    auth.main(["list"])
    listing = capsys.readouterr().out
    assert "phone" in listing
    auth.main(["revoke", "--name", "phone"])
    assert "revoked phone" in capsys.readouterr().out
    client.close()


def test_cli_errors(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    _patch(monkeypatch, settings, client)
    auth.main(["create", "--name", "phone"])
    with pytest.raises(SystemExit):
        auth.main(["create", "--name", "phone"])
    assert "already exists" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        auth.main(["revoke", "--name", "missing"])
    assert "not found" in capsys.readouterr().err
    client.close()


def test_cli_list_empty(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    _patch(monkeypatch, settings, client)
    auth.main(["list"])
    assert "no tokens" in capsys.readouterr().out
    client.close()
