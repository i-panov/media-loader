from __future__ import annotations

import logging
import threading
import time
from typing import Any

import redis

from media_loader.config import Settings
from media_loader.errors import LeaseLostError
from media_loader.store import job_progress_key, source_progress_key

LOGGER = logging.getLogger(__name__)


class ProgressReporter:
    def __init__(
        self,
        settings: Settings,
        client: redis.Redis | None = None,
        throttle_seconds: float = 1.0,
    ) -> None:
        self.settings = settings
        self.client = client or redis.Redis.from_url(settings.redis_url, decode_responses=True)
        self._owns_client = client is None
        self.throttle_seconds = throttle_seconds
        self._last_update: dict[str, float] = {}

    def _write(self, key: str, data: dict[str, Any], force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_update.get(key, 0.0) < self.throttle_seconds:
            return
        self._last_update[key] = now
        mapping = {name: str(value) for name, value in data.items() if value is not None}
        if not mapping:
            return
        try:
            pipe = self.client.pipeline()
            pipe.hset(key, mapping=mapping)
            pipe.expire(key, self.settings.job_timeout + 60)
            pipe.execute()
        except Exception:
            LOGGER.debug("failed to write progress to Redis", exc_info=True)

    def source(self, source_key: str, **data: Any) -> None:
        force = bool(data.pop("_force", False))
        self._write(source_progress_key(source_key), data, force=force)

    def job(self, request_key: str, **data: Any) -> None:
        force = bool(data.pop("_force", False))
        self._write(job_progress_key(request_key), data, force=force)

    def clear_source(self, source_key: str) -> None:
        self.client.delete(source_progress_key(source_key))

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class DownloadProgressTracker:
    def __init__(
        self,
        reporter: ProgressReporter,
        source_key: str,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.reporter = reporter
        self.source_key = source_key
        self.cancel_event = cancel_event
        self.totals: dict[str, float] = {}
        self.downloaded: dict[str, float] = {}

    def _remember_requested_formats(self, info: dict[str, Any]) -> None:
        for item in info.get("requested_formats") or []:
            if not isinstance(item, dict):
                continue
            format_id = str(item.get("format_id") or "unknown")
            total = item.get("filesize") or item.get("filesize_approx")
            if total:
                self.totals[format_id] = float(total)

    def hook(self, status: dict[str, Any]) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise LeaseLostError
        info = status.get("info_dict") or {}
        if isinstance(info, dict):
            self._remember_requested_formats(info)
        download_status = status.get("status")
        format_id = str(
            status.get("format_id")
            or (info.get("format_id") if isinstance(info, dict) else None)
            or status.get("filename")
            or "current"
        )
        downloaded = float(status.get("downloaded_bytes") or 0)
        total = (
            status.get("total_bytes")
            or status.get("total_bytes_estimate")
            or (info.get("filesize") if isinstance(info, dict) else None)
            or (info.get("filesize_approx") if isinstance(info, dict) else None)
        )
        if total:
            self.totals[format_id] = float(total)
        self.downloaded[format_id] = downloaded

        if download_status == "finished":
            self.downloaded[format_id] = self.totals.get(format_id, downloaded)

        total_sum = sum(self.totals.values())
        downloaded_sum = sum(self.downloaded.values())
        percent: float | None = None
        if total_sum > 0:
            percent = max(0.0, min(100.0, downloaded_sum / total_sum * 100.0))
        elif total and total > 0:
            percent = max(0.0, min(100.0, downloaded / float(total) * 100.0))

        self.reporter.source(
            self.source_key,
            stage="downloading",
            percent=round(percent, 2) if percent is not None else None,
            downloaded_bytes=int(downloaded_sum) if downloaded_sum else None,
            total_bytes=int(total_sum) if total_sum else None,
            speed_bps=status.get("speed"),
            eta_seconds=status.get("eta"),
            _force=download_status in {"finished", "error"},
        )
