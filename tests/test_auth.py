from __future__ import annotations

from typing import Any

import fakeredis
import fakeredis.aioredis
import pytest

from media_loader.auth import (
    AUTH_NAMES_KEY,
    AUTH_TOKEN_PREFIX,
    authenticate_async,
    authenticate_sync,
    create_token,
    hash_token,
    list_tokens,
    revoke_token,
)


def test_token_cli_roundtrip_hashes_plaintext() -> None:
    client: Any = fakeredis.FakeRedis(decode_responses=True)
    token = create_token(client, "phone", "mlk_")
    assert token.startswith("mlk_")
    digest = hash_token(token)
    stored = client.hgetall(f"{AUTH_TOKEN_PREFIX}{digest}")
    assert stored["name"] == "phone"
    assert token not in "".join(stored.values())
    assert authenticate_sync(client, token, 60) is not None
    assert [item["name"] for item in list_tokens(client)] == ["phone"]
    assert revoke_token(client, "phone") is True
    assert authenticate_sync(client, token, 60) is None
    client.close()


def test_duplicate_name_is_rejected() -> None:
    client: Any = fakeredis.FakeRedis(decode_responses=True)
    create_token(client, "phone", "mlk_")
    with pytest.raises(ValueError):
        create_token(client, "phone", "mlk_")
    client.close()


async def test_async_authentication_updates_last_used() -> None:
    client: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    token = "mlk_async_test"
    digest = hash_token(token)
    await client.hset(AUTH_NAMES_KEY, "phone", digest)
    await client.hset(
        f"{AUTH_TOKEN_PREFIX}{digest}",
        mapping={
            "name": "phone",
            "token_id": digest[:12],
            "created_at": "2026-10-20T10:00:00+00:00",
            "last_used_at": "",
            "expires_at": "",
        },
    )
    record = await authenticate_async(client, token, 0)
    assert record is not None
    assert record["name"] == "phone"
    updated = await client.hget(f"{AUTH_TOKEN_PREFIX}{digest}", "last_used_at")
    assert updated
    assert await authenticate_async(client, "wrong", 0) is None
    await client.aclose()
