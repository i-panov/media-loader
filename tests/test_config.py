from __future__ import annotations

from pathlib import Path

from media_loader.config import Settings, parse_duration, parse_size


def test_parse_duration() -> None:
    assert parse_duration("24h") == 86400
    assert parse_duration("1h30m") == 5400
    assert parse_duration(90) == 90


def test_parse_size() -> None:
    assert parse_size("5GB") == 5 * 1024**3
    assert parse_size("512MiB") == 512 * 1024**2
    assert parse_size(100) == 100


def test_settings_human_values(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=tmp_path,
        cache_dir=tmp_path / "cache",
        job_dir=tmp_path / "jobs",
        tmp_dir=tmp_path / "tmp",
        cache_ttl="24h",  # type: ignore[arg-type]
        cache_max_size="5GB",  # type: ignore[arg-type]
        quality_levels="360p,720p,1080p",
    )
    assert settings.cache_ttl == 86400
    assert settings.cache_max_size == 5 * 1024**3
    assert settings.quality_level_set == {"360p", "720p", "1080p"}
