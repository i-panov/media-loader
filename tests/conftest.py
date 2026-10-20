from __future__ import annotations

from pathlib import Path

import pytest

from media_loader.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    value = Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=tmp_path,
        redis_url="redis://localhost:6379/0",
        auth_enabled=False,
        proxy_url="socks5://127.0.0.1:1080",
        cache_ttl=3600,
        cache_max_size=1024 * 1024,
        job_ttl=60,
        job_timeout=120,
        failed_retry_seconds=0,
        direct_socket_timeout=1,
        proxy_state_ttl=60,
        max_concurrent_jobs=1,
        max_concurrent_transcodes=1,
    )
    value.ensure_dirs()
    return value
