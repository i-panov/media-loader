from __future__ import annotations

import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

_ILLEGAL_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SPACES = re.compile(r"\s+")

CONTENT_TYPES = {
    "mp4": "video/mp4",
    "mkv": "video/x-matroska",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
    "ac3": "audio/ac3",
    "eac3": "audio/eac3",
    "mka": "audio/x-matroska",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def local_display(value: datetime | str | None, timezone: ZoneInfo) -> str | None:
    if value is None:
        return None
    dt = parse_dt(value) if isinstance(value, str) else value
    if dt is None:
        return None
    return dt.astimezone(timezone).strftime("%Y-%m-%d %H:%M:%S")


def safe_filename(title: str, fallback: str, suffix: str, max_length: int = 180) -> str:
    cleaned = _ILLEGAL_FILENAME.sub("_", title or "").strip()
    cleaned = _SPACES.sub(" ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback or "media"
    suffix = suffix if suffix.startswith(".") else f".{suffix}" if suffix else ""
    budget = max(1, max_length - len(suffix))
    return f"{cleaned[:budget]}{suffix}"


def atomic_move(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    try:
        source.replace(destination)
    except OSError:
        shutil.move(str(source), str(destination))


def hardlink_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def is_within(base: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


def proxy_host_port(proxy_url: str) -> tuple[str, int]:
    parsed = urlsplit(proxy_url)
    host = parsed.hostname or "127.0.0.1"
    return host, parsed.port or 1080


def human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"
