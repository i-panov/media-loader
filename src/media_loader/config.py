from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([smhdSMHD]?)")
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtKMGT]?i?[bB]?)\s*$")
_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
}
_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid duration")
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if not text:
        raise ValueError("invalid duration")
    total = 0.0
    position = 0
    found = False
    for match in _DURATION_RE.finditer(text):
        if text[position : match.start()].strip():
            raise ValueError(f"invalid duration: {value!r}")
        amount = float(match.group(1))
        unit = match.group(2) or ""
        total += amount * _DURATION_UNITS[unit]
        position = match.end()
        found = True
    if not found or text[position:].strip():
        raise ValueError(f"invalid duration: {value!r}")
    return int(total)


def parse_size(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid size")
    if isinstance(value, (int, float)):
        return int(value)
    match = _SIZE_RE.match(str(value))
    if not match:
        raise ValueError(f"invalid size: {value!r}")
    amount = float(match.group(1))
    unit = match.group(2).lower()
    if unit not in _SIZE_UNITS:
        raise ValueError(f"invalid size unit: {value!r}")
    return int(amount * _SIZE_UNITS[unit])


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    server_timezone: str = "Europe/Moscow"
    log_level: str = "INFO"

    data_dir: Path = Path("/data")

    cache_ttl: int = 24 * 3600
    cache_max_size: int = 5 * 1024**3
    cache_cleanup_interval: int = 300
    min_free_disk: int = 1024**3

    job_ttl: int = 3600
    job_timeout: int = 12 * 3600
    source_lock_ttl: int = 120
    queued_reenqueue_seconds: int = 10
    max_concurrent_jobs: int = 2
    max_concurrent_transcodes: int = 1
    failed_retry_seconds: int = 60
    failed_record_ttl: int = 300
    retry_after_seconds: int = 5

    redis_url: str = "redis://redis:6379/0"
    proxy_url: str = "socks5://byedpi:1080"
    byedpi_strategies_file: Path = Path("/config/byedpi-strategies.conf")
    byedpi_base_port: int = 1080
    proxy_state_ttl: int = 24 * 3600
    proxy_strategy_ttl: int = 24 * 3600
    proxy_probe_timeout: int = 10
    proxy_probe_total_timeout: int = 90
    proxy_probe_lock_ttl: int = 120
    proxy_probe_lock_wait: int = 30
    proxy_probe_sample_bytes: int = 256 * 1024
    direct_socket_timeout: int = 15
    direct_probe_timeout: int = 5
    ytdlp_youtube_player_clients: str = "default,tv_embedded,web_embedded,mweb,android_vr"

    default_video_format: str = "mp4"
    default_audio_format: str = "mp3"
    quality_levels: str = "240p,360p,480p,720p,1080p,1440p,2160p"

    video_crf: int = 18
    video_preset: str = "medium"
    audio_aac_bitrate: str = "256k"
    audio_mp3_bitrate: str = "320k"
    audio_opus_bitrate: str = "160k"
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    ytdlp_cookies_file: Path = Path("/config/cookies.txt")

    auth_enabled: bool = True
    auth_token_prefix: str = "mlk_"
    auth_last_used_update_interval: int = 60
    docs_enabled: bool = False

    @field_validator(
        "cache_ttl",
        "cache_cleanup_interval",
        "job_ttl",
        "job_timeout",
        "source_lock_ttl",
        "queued_reenqueue_seconds",
        "proxy_state_ttl",
        "proxy_strategy_ttl",
        "proxy_probe_timeout",
        "proxy_probe_total_timeout",
        "proxy_probe_lock_ttl",
        "proxy_probe_lock_wait",
        "failed_retry_seconds",
        "failed_record_ttl",
        "retry_after_seconds",
        "auth_last_used_update_interval",
        mode="before",
    )
    @classmethod
    def _parse_duration_fields(cls, value: object) -> int:
        return parse_duration(value)

    @field_validator("proxy_probe_sample_bytes", "cache_max_size", "min_free_disk", mode="before")
    @classmethod
    def _parse_size_field(cls, value: object) -> int:
        return parse_size(value)

    @field_validator("server_timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.server_timezone)

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def job_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def metadata_db_path(self) -> Path:
        return self.data_dir / "metadata.sqlite3"

    @property
    def quality_level_set(self) -> set[str]:
        return {item.strip().lower() for item in self.quality_levels.split(",") if item.strip()}

    @property
    def youtube_player_clients(self) -> tuple[str | None, ...]:
        values = [item.strip() for item in self.ytdlp_youtube_player_clients.split(",")]
        clients: list[str | None] = []
        for value in values:
            if not value:
                continue
            clients.append(None if value.lower() in {"default", "auto"} else value)
        return tuple(clients) or (None,)

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.cache_dir, self.job_dir, self.tmp_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
