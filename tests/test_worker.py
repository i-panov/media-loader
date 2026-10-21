from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import fakeredis
import fakeredis.aioredis
import pytest
import redis.asyncio as aioredis

import media_loader.worker as worker
from media_loader.config import Settings
from media_loader.domain_state import DomainState
from media_loader.errors import DiskFullError, DownloadFailedError, LeaseLostError
from media_loader.metadata_db import MetadataDatabase
from media_loader.progress import ProgressReporter
from media_loader.proxy_strategies import ProxyStrategyStore
from media_loader.store import Store
from media_loader.transcode import MediaInfo
from media_loader.utils import iso, utcnow
from media_loader.ytdlp import DownloadResult, MediaMetadata


def _metadata(title: str = "Example") -> MediaMetadata:
    return MediaMetadata(
        title=title,
        description="Description",
        duration=10.0,
        thumbnail=None,
        has_video=True,
        has_audio=True,
        width=1280,
        height=720,
        max_quality="720p",
        available_qualities=("240p", "720p"),
        video_codec="h264",
        audio_codec="aac",
        details={"id": "abc", "uploader": "Uploader"},
    )


def _result(path: Path) -> DownloadResult:
    return DownloadResult(path=path, metadata=_metadata(), info={"title": "Example"})


def _job(request_key: str = "job1", source_key: str = "source1") -> dict[str, Any]:
    return {
        "request_key": request_key,
        "source_key": source_key,
        "url": "https://example.com/video",
        "canonical_url": "https://example.com/video",
        "media_type": "video",
        "format": "mkv",
        "quality": None,
        "source_cached": True,
        "status": "queued",
        "stage": "queued",
        "created_at": iso(utcnow()),
        "download_started_at": None,
        "processing_started_at": None,
        "finished_at": None,
        "title": "Example",
        "duration": 10.0,
        "thumbnail": None,
        "description": "Description",
        "available_qualities": ["240p", "720p"],
        "metadata_details": {"id": "abc", "uploader": "Uploader"},
        "source_quality": "720p",
        "actual_quality": None,
        "quality_clamped": False,
        "via_proxy": False,
        "attempts": 0,
        "error": None,
        "error_code": None,
        "http_status": None,
        "progress": {},
    }


def _source_record(path: Path, key: str = "source1") -> dict[str, Any]:
    now = utcnow()
    return {
        "source_key": key,
        "url": "https://example.com/video",
        "canonical_url": "https://example.com/video",
        "status": "ready",
        "created_at": iso(now),
        "download_started_at": iso(now),
        "finished_at": iso(now),
        "expires_at": iso(now + timedelta(hours=1)),
        "source_path": str(path),
        "source_filename": path.name,
        "source_ext": path.suffix.lstrip("."),
        "source_size": path.stat().st_size if path.exists() else 0,
        "has_video": True,
        "width": 1280,
        "height": 720,
        "source_quality": "720p",
        "video_codec": "h264",
        "audio_codec": "aac",
        "duration": 10.0,
        "title": "Example",
        "thumbnail": None,
        "description": "Description",
        "available_qualities": ["240p", "720p"],
        "metadata_details": {"id": "abc", "uploader": "Uploader"},
        "via_proxy": False,
        "attempts": 1,
        "error": None,
    }


def test_helpers(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x")
    assert worker._source_is_ready(_source_record(source)) is True
    record = _source_record(source)
    record["expires_at"] = iso(utcnow() - timedelta(seconds=1))
    assert worker._source_is_ready(record) is False
    record["status"] = "downloading"
    assert worker._source_is_ready(record) is False
    assert worker._title_for_output("Title", "video", "720p", "mp4") == "Title [720p].mp4"
    assert worker._title_for_output("Title", "audio", None, "mp3") == "Title.mp3"


def test_clear_directory(tmp_path: Path) -> None:
    directory = tmp_path / "dir"
    (directory / "sub").mkdir(parents=True)
    (directory / "file").write_text("x")
    (directory / "sub" / "nested").write_text("y")
    worker._clear_directory(directory)
    assert list(directory.iterdir()) == []


async def test_fail_job(tmp_path: Path, settings: Settings) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    job = _job()
    job_dir = store.job_dir("job1")
    job_dir.mkdir(parents=True)
    (job_dir / "output.mp4").write_text("x")
    await worker._fail_job(store, job, DownloadFailedError("boom"))
    stored = await store.get_job("job1")
    assert stored is not None
    assert stored["status"] == "failed"
    assert stored["http_status"] == 502
    assert stored["error"] == "boom"
    assert not job_dir.exists()
    await redis.aclose()


async def test_fail_job_disk_full(tmp_path: Path, settings: Settings) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    job = _job()
    await worker._fail_job(store, job, DiskFullError("no space"))
    stored = await store.get_job("job1")
    assert stored is not None
    assert stored["http_status"] == 507
    await redis.aclose()


async def test_download_with_fallback_cases(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    state = DomainState(redis, settings)
    reporter = ProgressReporter(settings, client=fakeredis.FakeRedis(decode_responses=True))
    calls: list[str | None] = []

    async def direct_ok(url: str, timeout: int) -> bool:
        del url, timeout
        return True

    async def direct_fail(url: str, timeout: int) -> bool:
        del url, timeout
        return False

    async def fake_strategy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {
            "strategy": "default",
            "proxy_url": str(settings.proxy_url),
            "player_client": None,
        }

    def download_ok(*args: Any, **kwargs: Any) -> DownloadResult:
        calls.append(args[2])
        return _result(tmp_path / "result.mkv")

    monkeypatch.setattr(worker, "direct_available", direct_ok)
    monkeypatch.setattr(worker, "download_source", download_ok)
    monkeypatch.setattr(worker, "ensure_proxy_strategy", fake_strategy)
    job = _job()
    result = await worker._download_with_fallback(
        job,
        "source1",
        "https://example.com/video",
        "example.com",
        tmp_path,
        settings,
        store,
        state,
        reporter,
        threading.Event(),
    )
    assert result.via_proxy is False
    assert calls == [None]
    assert await state.get_mode("example.com") == "direct"

    await state.delete("example.com")
    calls.clear()
    monkeypatch.setattr(worker, "direct_available", direct_fail)
    result = await worker._download_with_fallback(
        job,
        "source1",
        "https://example.com/video",
        "example.com",
        tmp_path,
        settings,
        store,
        state,
        reporter,
        threading.Event(),
    )
    assert result.via_proxy is True
    assert calls == [settings.proxy_url]
    assert await state.get_mode("example.com") == "proxy"

    await state.set("example.com", "proxy", "already")
    calls.clear()
    result = await worker._download_with_fallback(
        job,
        "source1",
        "https://example.com/video",
        "example.com",
        tmp_path,
        settings,
        store,
        state,
        reporter,
        threading.Event(),
    )
    assert result.via_proxy is True
    assert calls == [settings.proxy_url]

    await state.delete("example.com")
    monkeypatch.setattr(worker, "direct_available", direct_ok)
    attempts = 0

    def download_retry(*args: Any, **kwargs: Any) -> DownloadResult:
        nonlocal attempts
        attempts += 1
        calls.append(args[2])
        if attempts == 1:
            raise RuntimeError("connection reset")
        return _result(tmp_path / "result.mkv")

    calls.clear()
    monkeypatch.setattr(worker, "download_source", download_retry)
    result = await worker._download_with_fallback(
        job,
        "source1",
        "https://example.com/video",
        "example.com",
        tmp_path,
        settings,
        store,
        state,
        reporter,
        threading.Event(),
    )
    assert result.via_proxy is True
    assert calls == [None, settings.proxy_url]

    def download_permanent(*args: Any, **kwargs: Any) -> DownloadResult:
        raise RuntimeError("unsupported url")

    monkeypatch.setattr(worker, "download_source", download_permanent)
    with pytest.raises(DownloadFailedError):
        await worker._download_with_fallback(
            job,
            "source1",
            "https://example.com/video",
            "example.com",
            tmp_path,
            settings,
            store,
            state,
            reporter,
            threading.Event(),
        )
    reporter.close()
    await redis.aclose()


async def test_proxy_strategy_retries_transient_error_without_reprobe(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    strategy_store = ProxyStrategyStore(redis, settings)
    reporter = ProgressReporter(settings, client=fakeredis.FakeRedis(decode_responses=True))
    record = {
        "host": "example.com",
        "strategy": "default",
        "proxy_url": str(settings.proxy_url),
        "player_client": None,
    }
    await strategy_store.set("example.com", record)
    attempts = 0
    ensure_calls = 0

    async def fake_ensure(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal ensure_calls
        ensure_calls += 1
        return record

    def fake_download(*args: Any, **kwargs: Any) -> DownloadResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("connection reset by peer")
        path = tmp_path / "result.mkv"
        path.write_bytes(b"result")
        return _result(path)

    monkeypatch.setattr(worker, "ensure_proxy_strategy", fake_ensure)
    monkeypatch.setattr(worker, "download_source", fake_download)
    result, _ = await worker._download_with_proxy_strategy(
        _job(),
        "source1",
        "https://example.com/video",
        "example.com",
        tmp_path,
        settings,
        store,
        strategy_store,
        reporter,
        threading.Event(),
    )
    assert result.path.name == "result.mkv"
    assert attempts == 2
    assert ensure_calls == 0
    assert await strategy_store.get("example.com") is not None
    reporter.close()
    await redis.aclose()


async def test_proxy_strategy_reprobes_after_two_transport_errors(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    strategy_store = ProxyStrategyStore(redis, settings)
    reporter = ProgressReporter(settings, client=fakeredis.FakeRedis(decode_responses=True))
    old = {
        "host": "example.com",
        "strategy": "old",
        "proxy_url": "socks5://old:1080",
        "player_client": None,
    }
    new = {
        "host": "example.com",
        "strategy": "new",
        "proxy_url": "socks5://new:1081",
        "player_client": "tv_embedded",
    }
    await strategy_store.set("example.com", old)
    proxy_urls: list[str] = []
    attempts = 0
    ensure_calls = 0

    async def fake_ensure(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal ensure_calls
        ensure_calls += 1
        assert await strategy_store.get("example.com") is None
        await strategy_store.set("example.com", new)
        return new

    def fake_download(*args: Any, **kwargs: Any) -> DownloadResult:
        nonlocal attempts
        attempts += 1
        proxy_urls.append(str(args[2]))
        if attempts < 3:
            raise RuntimeError("connection reset by peer")
        path = tmp_path / "result.mkv"
        path.write_bytes(b"result")
        return _result(path)

    monkeypatch.setattr(worker, "ensure_proxy_strategy", fake_ensure)
    monkeypatch.setattr(worker, "download_source", fake_download)
    result, strategy = await worker._download_with_proxy_strategy(
        _job(),
        "source1",
        "https://example.com/video",
        "example.com",
        tmp_path,
        settings,
        store,
        strategy_store,
        reporter,
        threading.Event(),
    )
    assert result.path.name == "result.mkv"
    assert attempts == 3
    assert ensure_calls == 1
    assert proxy_urls == [old["proxy_url"], old["proxy_url"], new["proxy_url"]]
    assert strategy["strategy"] == "new"
    reporter.close()
    await redis.aclose()


async def test_proxy_strategy_keeps_strategy_on_non_transport_error(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    strategy_store = ProxyStrategyStore(redis, settings)
    reporter = ProgressReporter(settings, client=fakeredis.FakeRedis(decode_responses=True))
    record = {
        "host": "example.com",
        "strategy": "default",
        "proxy_url": str(settings.proxy_url),
        "player_client": None,
    }
    await strategy_store.set("example.com", record)

    def fake_download(*args: Any, **kwargs: Any) -> DownloadResult:
        raise RuntimeError("disk full")

    monkeypatch.setattr(worker, "download_source", fake_download)
    with pytest.raises(RuntimeError, match="disk full"):
        await worker._download_with_proxy_strategy(
            _job(),
            "source1",
            "https://example.com/video",
            "example.com",
            tmp_path,
            settings,
            store,
            strategy_store,
            reporter,
            threading.Event(),
        )
    assert await strategy_store.get("example.com") is not None
    reporter.close()
    await redis.aclose()


async def test_store_source_success(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.min_free_disk = 0
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    state = DomainState(redis, settings)
    db = MetadataDatabase(settings.metadata_db_path)
    reporter = ProgressReporter(settings, client=fakeredis.FakeRedis(decode_responses=True))

    async def fake_fallback(
        job: dict[str, Any],
        source_key: str,
        canonical_url: str,
        host: str,
        temp_dir: Path,
        *args: Any,
        **kwargs: Any,
    ) -> worker.AttemptResult:
        del job, source_key, canonical_url, host, args, kwargs
        path = temp_dir / "download.mkv"
        path.write_bytes(b"source")
        return worker.AttemptResult(_result(path), via_proxy=True)

    async def owns_lock(name: str, token: str) -> bool:
        del name, token
        return True

    monkeypatch.setattr(store, "owns_lock", owns_lock)

    async def fake_probe(path: Path, settings: Settings) -> MediaInfo:
        del settings
        return MediaInfo(
            path=path,
            has_video=True,
            has_audio=True,
            width=1280,
            height=720,
            video_codec="h264",
            audio_codec="aac",
            duration=10.0,
            format_name="matroska",
        )

    monkeypatch.setattr(worker, "_download_with_fallback", fake_fallback)
    monkeypatch.setattr(worker, "probe_media", fake_probe)
    job = _job()
    record = await worker._store_source(
        job,
        "source1",
        "https://example.com/video",
        "example.com",
        settings,
        store,
        state,
        reporter,
        db,
        "source:source1",
        "token",
        threading.Event(),
    )
    assert record["status"] == "ready"
    assert Path(record["source_path"]).is_file()
    assert record["via_proxy"] is True
    assert record["available_qualities"] == ["240p", "720p"]
    stored = await store.get_source("source1")
    assert stored is not None
    assert stored["title"] == "Example"
    db_metadata = await db.get("source1")
    assert db_metadata is not None
    assert db_metadata["title"] == "Example"
    reporter.close()
    await db.close()
    await redis.aclose()


async def test_store_source_disk_full(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.min_free_disk = 10**18
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    state = DomainState(redis, settings)
    db = MetadataDatabase(settings.metadata_db_path)
    reporter = ProgressReporter(settings, client=fakeredis.FakeRedis(decode_responses=True))
    with pytest.raises(DiskFullError):
        await worker._store_source(
            _job(),
            "source1",
            "https://example.com/video",
            "example.com",
            settings,
            store,
            state,
            reporter,
            db,
            "source:source1",
            "token",
            threading.Event(),
        )
    source = await store.get_source("source1")
    assert source is not None
    assert source["status"] == "failed"
    reporter.close()
    await db.close()
    await redis.aclose()


async def test_ensure_source_ready(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    state = DomainState(redis, settings)
    db = MetadataDatabase(settings.metadata_db_path)
    reporter = ProgressReporter(settings, client=fakeredis.FakeRedis(decode_responses=True))
    source_path = settings.cache_dir / "source1" / "source.mkv"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"x")
    record = _source_record(source_path)
    await store.save_source(record)
    await store.mark_source_ready(record)
    result = await worker.ensure_source(
        _job(),
        "source1",
        "https://example.com/video",
        settings,
        store,
        state,
        reporter,
        db,
    )
    assert result["status"] == "ready"
    reporter.close()
    await db.close()
    await redis.aclose()


async def test_derive_copy_and_command(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    source_path = settings.cache_dir / "source1" / "source.mkv"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source")
    source = _source_record(source_path)
    reporter = ProgressReporter(settings, client=fakeredis.FakeRedis(decode_responses=True))

    async def fake_probe(path: Path, settings: Settings) -> MediaInfo:
        del path, settings
        return MediaInfo(
            path=source_path,
            has_video=True,
            has_audio=True,
            width=1280,
            height=720,
            video_codec="h264",
            audio_codec="aac",
            duration=10.0,
            format_name="matroska",
        )

    monkeypatch.setattr(worker, "probe_media", fake_probe)
    job = _job()
    await worker._derive(job, source, settings, store, reporter)
    assert job["status"] == "ready"
    assert Path(job["output_path"]).is_file()
    assert job["content_type"] == "video/x-matroska"

    async def fake_run_ffmpeg(command: list[str], *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        Path(command[-1]).write_bytes(b"derived")

    monkeypatch.setattr(worker, "run_ffmpeg", fake_run_ffmpeg)
    job = _job(request_key="job2")
    job["format"] = "mp4"
    await worker._derive(job, source, settings, store, reporter)
    assert job["status"] == "ready"
    assert Path(job["output_path"]).is_file()
    assert job["format"] == "mp4"
    reporter.close()
    await redis.aclose()


async def test_process_request_success_and_failure(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    state = DomainState(redis, settings)
    db = MetadataDatabase(settings.metadata_db_path)
    source_path = settings.cache_dir / "source1" / "source.mkv"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source")
    source = _source_record(source_path)
    job = _job(request_key="job1")
    await store.save_job(job)
    ctx = {
        "settings": settings,
        "store": store,
        "domain_state": state,
        "metadata_db": db,
        "transcode_sem": asyncio.Semaphore(1),
    }

    async def fake_ensure(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return source

    async def fake_derive(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(worker, "ensure_source", fake_ensure)
    monkeypatch.setattr(worker, "_derive", fake_derive)
    await worker.process_request(ctx, "job1")
    assert await store.get_active("source1") == 0

    job2 = _job(request_key="job2")
    await store.save_job(job2)

    async def fail_ensure(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise DownloadFailedError("failed")

    monkeypatch.setattr(worker, "ensure_source", fail_ensure)
    await worker.process_request(ctx, "job2")
    stored = await store.get_job("job2")
    assert stored is not None
    assert stored["status"] == "failed"
    assert await store.get_active("source1") == 0
    await db.close()
    await redis.aclose()


def test_cleanup_schedule() -> None:
    assert worker._cleanup_schedule(300) == (None, {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55})
    assert worker._cleanup_schedule(7200) == (set(range(0, 24, 2)), {0})
    assert worker._cleanup_schedule(86400) == ({0}, {0})


async def test_cleanup_task(settings: Settings) -> None:
    called: list[str] = []

    class FakeStore:
        async def cleanup_sources(self) -> dict[str, int]:
            called.append("sources")
            return {}

        async def cleanup_jobs(self) -> int:
            called.append("jobs")
            return 0

    await worker.cleanup_task({"settings": settings, "store": FakeStore()})
    assert called == ["sources", "jobs"]


def _audio_result(path: Path) -> DownloadResult:
    metadata = MediaMetadata(
        title="Audio",
        description=None,
        duration=10.0,
        thumbnail=None,
        has_video=False,
        has_audio=True,
        width=None,
        height=None,
        max_quality=None,
        available_qualities=(),
        video_codec=None,
        audio_codec="aac",
        details={"id": "audio"},
    )
    return DownloadResult(path=path, metadata=metadata, info={})


async def test_store_source_remux_and_audio(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.min_free_disk = 0
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    state = DomainState(redis, settings)
    db = MetadataDatabase(settings.metadata_db_path)
    reporter = ProgressReporter(settings, client=fakeredis.FakeRedis(decode_responses=True))

    async def owns_lock(name: str, token: str) -> bool:
        del name, token
        return True

    monkeypatch.setattr(store, "owns_lock", owns_lock)

    async def fake_fallback_mp4(
        job: dict[str, Any],
        source_key: str,
        canonical_url: str,
        host: str,
        temp_dir: Path,
        *args: Any,
        **kwargs: Any,
    ) -> worker.AttemptResult:
        del job, source_key, canonical_url, host, args, kwargs
        path = temp_dir / "download.mp4"
        path.write_bytes(b"video")
        return worker.AttemptResult(_result(path), via_proxy=False)

    async def fake_remux(source: Path, destination: Path, settings: Settings) -> None:
        del source, settings
        destination.write_bytes(b"remuxed")

    async def fake_probe(path: Path, settings: Settings) -> MediaInfo:
        del settings
        video = path.suffix != ".m4a"
        return MediaInfo(
            path=path,
            has_video=video,
            has_audio=True,
            width=1280 if video else None,
            height=720 if video else None,
            video_codec="h264" if video else None,
            audio_codec="aac",
            duration=10.0,
            format_name="matroska",
        )

    monkeypatch.setattr(worker, "_download_with_fallback", fake_fallback_mp4)
    monkeypatch.setattr(worker, "remux_to_mkv", fake_remux)
    monkeypatch.setattr(worker, "probe_media", fake_probe)
    record = await worker._store_source(
        _job(),
        "source-video",
        "https://example.com/video",
        "example.com",
        settings,
        store,
        state,
        reporter,
        db,
        "source:source-video",
        "token",
        threading.Event(),
    )
    assert Path(record["source_path"]).suffix == ".mkv"
    assert record["has_video"] is True

    async def fake_fallback_audio(
        job: dict[str, Any],
        source_key: str,
        canonical_url: str,
        host: str,
        temp_dir: Path,
        *args: Any,
        **kwargs: Any,
    ) -> worker.AttemptResult:
        del job, source_key, canonical_url, host, args, kwargs
        path = temp_dir / "download.m4a"
        path.write_bytes(b"audio")
        return worker.AttemptResult(_audio_result(path), via_proxy=False)

    monkeypatch.setattr(worker, "_download_with_fallback", fake_fallback_audio)
    audio_record = await worker._store_source(
        _job(request_key="job-audio", source_key="source-audio"),
        "source-audio",
        "https://example.com/audio",
        "example.com",
        settings,
        store,
        state,
        reporter,
        db,
        "source:source-audio",
        "token",
        threading.Event(),
    )
    assert Path(audio_record["source_path"]).suffix == ".m4a"
    assert audio_record["has_video"] is False
    reporter.close()
    await db.close()
    await redis.aclose()


async def test_startup_shutdown_and_early_returns(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    stale = _job(request_key="stale")
    stale["status"] = "processing"
    await store.save_job(stale)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(aioredis.Redis, "from_url", lambda *args, **kwargs: redis)
    ctx: dict[str, Any] = {}
    await worker.startup(ctx)
    assert ctx["settings"] is settings
    assert ctx["metadata_db"] is not None
    recovered = await store.get_job("stale")
    assert recovered is not None
    assert recovered["status"] == "failed"
    await worker.shutdown(ctx)

    db = MetadataDatabase(settings.metadata_db_path)
    ctx = {
        "settings": settings,
        "store": store,
        "domain_state": DomainState(redis, settings),
        "metadata_db": db,
        "transcode_sem": asyncio.Semaphore(1),
    }
    await worker.process_request(ctx, "missing")
    ready = _job(request_key="ready")
    ready["status"] = "ready"
    await store.save_job(ready)
    await worker.process_request(ctx, "ready")
    await db.close()
    await redis.aclose()


async def test_ensure_source_waiting_for_other_job(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    state = DomainState(redis, settings)
    db = MetadataDatabase(settings.metadata_db_path)
    reporter = ProgressReporter(settings, client=fakeredis.FakeRedis(decode_responses=True))
    source_path = settings.cache_dir / "source1" / "source.mkv"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"source")
    downloading = _source_record(source_path)
    downloading["status"] = "downloading"
    downloading["expires_at"] = None
    await store.save_source(downloading)
    lock = await store.acquire_lock("source:source1", 60)
    assert lock is not None
    ready = _source_record(source_path)
    setup_done = False

    async def fake_sleep(seconds: float) -> None:
        nonlocal setup_done
        del seconds
        if setup_done:
            return
        setup_done = True
        await store.save_source(ready)
        await store.mark_source_ready(ready)
        await redis.delete("ml:lock:source:source1")

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    result = await worker.ensure_source(
        _job(),
        "source1",
        "https://example.com/video",
        settings,
        store,
        state,
        reporter,
        db,
    )
    assert result["status"] == "ready"
    reporter.close()
    await db.close()
    await redis.aclose()


async def test_process_request_unexpected_error(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    db = MetadataDatabase(settings.metadata_db_path)
    job = _job(request_key="job-error")
    await store.save_job(job)
    ctx = {
        "settings": settings,
        "store": store,
        "domain_state": DomainState(redis, settings),
        "metadata_db": db,
        "transcode_sem": asyncio.Semaphore(1),
    }

    async def explode(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(worker, "ensure_source", explode)
    await worker.process_request(ctx, "job-error")
    stored = await store.get_job("job-error")
    assert stored is not None
    assert stored["status"] == "failed"
    assert stored["error_code"] == "internal_error"
    await db.close()
    await redis.aclose()


async def test_cleanup_task_failure() -> None:
    class FakeStore:
        async def cleanup_sources(self) -> dict[str, int]:
            raise RuntimeError("boom")

        async def cleanup_jobs(self) -> int:
            return 0

    await worker.cleanup_task({"store": FakeStore()})


async def test_startup_warns_without_tokens(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings.auth_enabled = True
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(aioredis.Redis, "from_url", lambda *args, **kwargs: redis)
    ctx: dict[str, Any] = {}
    with caplog.at_level("WARNING"):
        await worker.startup(ctx)
    assert "no API tokens configured" in caplog.text
    await worker.shutdown(ctx)
    await worker.shutdown({})
    await redis.aclose()


async def test_renew_lease_until_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeStore:
        async def renew_lock(self, name: str, token: str, ttl: int) -> bool:
            del token, ttl
            calls.append(name)
            return len(calls) < 2

    async def fake_sleep(seconds: float) -> None:
        del seconds

    cancel_event = threading.Event()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await worker._renew_lease(
        FakeStore(),  # type: ignore[arg-type]
        "lock1",
        "token",
        3,
        cancel_event,
    )
    assert len(calls) == 2
    assert cancel_event.is_set() is True


async def test_ensure_source_acquire_heartbeat_path(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    state = DomainState(redis, settings)
    db = MetadataDatabase(settings.metadata_db_path)
    reporter = ProgressReporter(settings, client=fakeredis.FakeRedis(decode_responses=True))
    source_path = settings.cache_dir / "source1" / "source.mkv"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"source")
    ready = _source_record(source_path)
    released: list[str] = []

    async def fake_store_source(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return ready

    async def fake_release(name: str, token: str) -> None:
        del token
        released.append(name)

    monkeypatch.setattr(worker, "_store_source", fake_store_source)
    monkeypatch.setattr(store, "release_lock", fake_release)
    result = await worker.ensure_source(
        _job(),
        "source1",
        "https://example.com/video",
        settings,
        store,
        state,
        reporter,
        db,
    )
    assert result["status"] == "ready"
    assert released == ["source:source1"]
    reporter.close()
    await db.close()
    await redis.aclose()


async def test_store_source_lost_lease_does_not_publish(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.min_free_disk = 0
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    state = DomainState(redis, settings)
    db = MetadataDatabase(settings.metadata_db_path)
    reporter = ProgressReporter(settings, client=fakeredis.FakeRedis(decode_responses=True))

    async def fake_fallback(
        job: dict[str, Any],
        source_key: str,
        canonical_url: str,
        host: str,
        temp_dir: Path,
        *args: Any,
        **kwargs: Any,
    ) -> worker.AttemptResult:
        del job, source_key, canonical_url, host, args, kwargs
        path = temp_dir / "download.mkv"
        path.write_bytes(b"source")
        return worker.AttemptResult(_result(path), via_proxy=False)

    async def fake_probe(path: Path, settings: Settings) -> MediaInfo:
        del settings
        return MediaInfo(
            path=path,
            has_video=True,
            has_audio=True,
            width=1280,
            height=720,
            video_codec="h264",
            audio_codec="aac",
            duration=10.0,
            format_name="matroska",
        )

    async def lost_lock(name: str, token: str) -> bool:
        del name, token
        return False

    monkeypatch.setattr(worker, "_download_with_fallback", fake_fallback)
    monkeypatch.setattr(worker, "probe_media", fake_probe)
    monkeypatch.setattr(store, "owns_lock", lost_lock)
    with pytest.raises(LeaseLostError):
        await worker._store_source(
            _job(),
            "source1",
            "https://example.com/video",
            "example.com",
            settings,
            store,
            state,
            reporter,
            db,
            "source:source1",
            "token",
            threading.Event(),
        )
    assert not (store.source_dir("source1") / "source.mkv").exists()
    reporter.close()
    await db.close()
    await redis.aclose()


async def test_process_request_ignores_stale_arq_token(
    settings: Settings,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, settings)
    db = MetadataDatabase(settings.metadata_db_path)
    job = _job(request_key="token-job")
    job["arq_token"] = "new-token"
    await store.save_job(job)
    ctx = {
        "settings": settings,
        "store": store,
        "domain_state": DomainState(redis, settings),
        "metadata_db": db,
        "transcode_sem": asyncio.Semaphore(1),
    }
    await worker.process_request(ctx, "token-job", "old-token")
    stored = await store.get_job("token-job")
    assert stored is not None
    assert stored["status"] == "queued"
    await db.close()
    await redis.aclose()
