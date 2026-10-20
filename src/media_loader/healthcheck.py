from __future__ import annotations

import socket
import sqlite3
import sys

import redis

from media_loader.config import get_settings
from media_loader.utils import proxy_host_port


def main() -> None:
    settings = get_settings()
    failures: list[str] = []
    try:
        client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        try:
            client.ping()
        finally:
            client.close()
    except Exception as exc:
        failures.append(f"redis: {exc}")

    try:
        if not settings.metadata_db_path.exists():
            raise FileNotFoundError(settings.metadata_db_path)
        connection = sqlite3.connect(settings.metadata_db_path)
        try:
            connection.execute("SELECT 1").fetchone()
        finally:
            connection.close()
    except Exception as exc:
        failures.append(f"sqlite: {exc}")

    host, port = proxy_host_port(settings.proxy_url)
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
    except Exception as exc:
        failures.append(f"byedpi: {exc}")

    if failures:
        print("; ".join(failures), file=sys.stderr)
        raise SystemExit(1)
    print("ok")
