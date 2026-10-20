from __future__ import annotations

import argparse
import hashlib
import secrets
import sys
from datetime import UTC, datetime
from typing import Any

import redis

from media_loader.config import get_settings
from media_loader.utils import iso, parse_dt

AUTH_NAMES_KEY = "ml:auth:names"
AUTH_TOKEN_PREFIX = "ml:auth:token:"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token_key(digest: str) -> str:
    return f"{AUTH_TOKEN_PREFIX}{digest}"


def _record(digest: str, name: str) -> dict[str, str]:
    now = iso(datetime.now(UTC))
    return {
        "name": name,
        "token_id": digest[:12],
        "created_at": now,
        "last_used_at": "",
        "expires_at": "",
    }


def create_token(client: Any, name: str, prefix: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("token name must not be empty")
    token = f"{prefix}{secrets.token_urlsafe(32)}"
    digest = hash_token(token)
    if not client.hsetnx(AUTH_NAMES_KEY, name, digest):
        raise ValueError(f"token {name!r} already exists")
    try:
        client.hset(_token_key(digest), mapping=_record(digest, name))
    except Exception:
        client.hdel(AUTH_NAMES_KEY, name)
        raise
    return token


def list_tokens(client: Any) -> list[dict[str, str]]:
    names = client.hgetall(AUTH_NAMES_KEY)
    result: list[dict[str, str]] = []
    for name, digest in sorted(names.items()):
        record = client.hgetall(_token_key(str(digest)))
        if record:
            result.append({str(key): str(value) for key, value in record.items()})
        else:
            client.hdel(AUTH_NAMES_KEY, str(name))
    return result


def revoke_token(client: Any, name: str) -> bool:
    digest = client.hget(AUTH_NAMES_KEY, name)
    if not digest:
        return False
    client.delete(_token_key(str(digest)))
    client.hdel(AUTH_NAMES_KEY, name)
    return True


def _is_expired(record: dict[str, Any]) -> bool:
    expires = parse_dt(str(record.get("expires_at") or ""))
    return bool(expires and expires <= datetime.now(UTC))


def _needs_last_used_update(record: dict[str, Any], interval: int) -> bool:
    last = parse_dt(str(record.get("last_used_at") or ""))
    return last is None or (datetime.now(UTC) - last).total_seconds() >= interval


def authenticate_sync(
    client: Any,
    token: str,
    update_interval: int = 60,
) -> dict[str, str] | None:
    digest = hash_token(token)
    key = _token_key(digest)
    record = client.hgetall(key)
    if not record:
        return None
    if _is_expired(record):
        return None
    if _needs_last_used_update(record, update_interval):
        client.hset(key, "last_used_at", iso(datetime.now(UTC)))
    return {str(k): str(v) for k, v in record.items()}


async def authenticate_async(
    client: Any,
    token: str,
    update_interval: int = 60,
) -> dict[str, str] | None:
    digest = hash_token(token)
    key = _token_key(digest)
    record = await client.hgetall(key)
    if not record:
        return None
    if _is_expired(record):
        return None
    if _needs_last_used_update(record, update_interval):
        await client.hset(key, "last_used_at", iso(datetime.now(UTC)))
    return {str(k): str(v) for k, v in record.items()}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="media-loader-auth", description="Manage API tokens")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create a named token")
    create.add_argument("--name", required=True)
    create.add_argument("--quiet", action="store_true", help="print only the token")

    sub.add_parser("list", help="list tokens")

    revoke = sub.add_parser("revoke", help="revoke a token by name")
    revoke.add_argument("--name", required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        if args.command == "create":
            try:
                token = create_token(client, args.name, settings.auth_token_prefix)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                raise SystemExit(1) from exc
            if args.quiet:
                print(token)
            else:
                print(token)
                print("Store this token now. It cannot be shown again.", file=sys.stderr)
        elif args.command == "list":
            tokens = list_tokens(client)
            if not tokens:
                print("no tokens")
            for item in tokens:
                print(
                    f"{item.get('name', ''):24} "
                    f"{item.get('token_id', ''):12} "
                    f"created={item.get('created_at', '')} "
                    f"last_used={item.get('last_used_at', '') or '-'}"
                )
        elif args.command == "revoke":
            if revoke_token(client, args.name):
                print(f"revoked {args.name}")
            else:
                print(f"token {args.name!r} not found", file=sys.stderr)
                raise SystemExit(1)
    finally:
        client.close()
