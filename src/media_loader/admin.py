from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import redis

from media_loader.config import get_settings
from media_loader.domain_state import DOMAIN_PREFIX
from media_loader.proxy_strategies import STRATEGY_PREFIX, strategy_key
from media_loader.store import SOURCE_SIZE_KEY, SOURCE_TTL_ZSET
from media_loader.utils import human_size


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except FileNotFoundError:
                continue
    return total


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="media-loader-admin", description="Media cache admin")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("cache-stats", help="show source cache and job usage")
    reset = sub.add_parser("proxy-reset", help="forget direct/proxy state and strategy")
    reset.add_argument("host")
    sub.add_parser("proxy-strategies", help="list cached per-host proxy strategies")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    client: Any = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        if args.command == "cache-stats":
            count = int(client.zcard(SOURCE_TTL_ZSET))
            size = int(client.get(SOURCE_SIZE_KEY) or 0)
            expired = int(client.zcount(SOURCE_TTL_ZSET, "-inf", time.time()))
            metadata_rows = 0
            if settings.metadata_db_path.exists():
                try:
                    connection = sqlite3.connect(settings.metadata_db_path)
                    try:
                        metadata_rows = int(
                            connection.execute("SELECT COUNT(*) FROM media_metadata").fetchone()[0]
                        )
                    finally:
                        connection.close()
                except sqlite3.Error:
                    metadata_rows = -1
            job_dir_size = _dir_size(settings.job_dir)
            tmp_dir_size = _dir_size(settings.tmp_dir)
            payload = {
                "source_count": count,
                "source_size": size,
                "source_size_human": human_size(size),
                "source_limit": settings.cache_max_size,
                "source_limit_human": human_size(settings.cache_max_size),
                "expired_sources": expired,
                "job_dir_size": job_dir_size,
                "job_dir_size_human": human_size(job_dir_size),
                "tmp_dir_size": tmp_dir_size,
                "tmp_dir_size_human": human_size(tmp_dir_size),
                "metadata_db_path": str(settings.metadata_db_path),
                "metadata_db_size": (
                    settings.metadata_db_path.stat().st_size
                    + (
                        settings.metadata_db_path.with_name(settings.metadata_db_path.name + "-wal")
                        .stat()
                        .st_size
                        if settings.metadata_db_path.with_name(
                            settings.metadata_db_path.name + "-wal"
                        ).exists()
                        else 0
                    )
                    if settings.metadata_db_path.exists()
                    else 0
                ),
                "metadata_rows": metadata_rows,
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        elif args.command == "proxy-reset":
            host = args.host.lower()
            deleted = client.delete(
                f"{DOMAIN_PREFIX}{host}",
                strategy_key(host),
            )
            print(f"reset {args.host}: {'ok' if deleted else 'no state'}")
        elif args.command == "proxy-strategies":
            records: list[dict[str, Any]] = []
            for key in sorted(client.scan_iter(match=f"{STRATEGY_PREFIX}*")):
                raw = client.get(key)
                if raw:
                    records.append(json.loads(raw))
            print(json.dumps(records, indent=2, ensure_ascii=False))
    finally:
        client.close()
