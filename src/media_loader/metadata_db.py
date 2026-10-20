from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from media_loader.utils import iso, utcnow


class MetadataDatabase:
    """Durable JSON storage for media metadata."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS media_metadata (
                    source_key TEXT PRIMARY KEY,
                    canonical_url TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_media_metadata_canonical_url
                ON media_metadata (canonical_url)
                """
            )
            self._conn.commit()

    def _get_sync(self, source_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT metadata_json FROM media_metadata WHERE source_key = ?",
                (source_key,),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row["metadata_json"]))
        return value if isinstance(value, dict) else None

    def _upsert_sync(
        self,
        source_key: str,
        canonical_url: str,
        metadata: dict[str, Any],
    ) -> None:
        now = iso(utcnow())
        payload = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO media_metadata
                    (source_key, canonical_url, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    canonical_url = excluded.canonical_url,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (source_key, canonical_url, payload, now, now),
            )
            self._conn.commit()

    def _ping_sync(self) -> bool:
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()
        return True

    async def ping(self) -> bool:
        return await asyncio.to_thread(self._ping_sync)

    async def get(self, source_key: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_sync, source_key)

    async def upsert(
        self,
        source_key: str,
        canonical_url: str,
        metadata: dict[str, Any],
    ) -> None:
        await asyncio.to_thread(
            self._upsert_sync,
            source_key,
            canonical_url,
            metadata,
        )

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()
