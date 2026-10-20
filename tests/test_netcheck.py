from __future__ import annotations

import asyncio
from typing import Any

import fakeredis.aioredis
import pytest

import media_loader.netcheck as netcheck
from media_loader.config import Settings
from media_loader.domain_state import DomainState
from media_loader.errors import DownloadFailedError
from media_loader.ytdlp import MediaMetadata


def _metadata() -> MediaMetadata:
    return MediaMetadata(
        title="Example",
        description="Text",
        duration=10.0,
        thumbnail=None,
        has_video=True,
        has_audio=True,
        width=1280,
        height=720,
        max_quality="720p",
        available_qualities=("720p",),
        video_codec="h264",
        audio_codec="aac",
        details={"id": "abc"},
    )


async def test_direct_available_local_server() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        del reader
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    assert server.sockets is not None
    port = server.sockets[0].getsockname()[1]
    assert await netcheck.direct_available(f"http://127.0.0.1:{port}", 2) is True
    server.close()
    await server.wait_closed()
    assert await netcheck.direct_available("http://127.0.0.1:1", 1) is False


async def test_metadata_proxy_mode(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    state = DomainState(redis, settings)
    await state.set("example.com", "proxy", "blocked")
    calls: list[str | None] = []

    async def fake_probe(url: str, proxy: str | None, *args: Any, **kwargs: Any) -> MediaMetadata:
        del url, args, kwargs
        calls.append(proxy)
        return _metadata()

    monkeypatch.setattr(netcheck, "_probe", fake_probe)
    result = await netcheck.fetch_metadata_with_fallback(
        "https://example.com/video", "example.com", settings, state
    )
    assert result.title == "Example"
    assert calls == [settings.proxy_url]
    await redis.aclose()


async def test_metadata_probe_failure_marks_proxy(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    state = DomainState(redis, settings)
    calls: list[str | None] = []

    async def fake_direct(url: str, timeout: int) -> bool:
        del url, timeout
        return False

    async def fake_probe(url: str, proxy: str | None, *args: Any, **kwargs: Any) -> MediaMetadata:
        del url, args, kwargs
        calls.append(proxy)
        return _metadata()

    monkeypatch.setattr(netcheck, "direct_available", fake_direct)
    monkeypatch.setattr(netcheck, "_probe", fake_probe)
    await netcheck.fetch_metadata_with_fallback(
        "https://example.com/video", "example.com", settings, state
    )
    assert calls == [settings.proxy_url]
    assert await state.get_mode("example.com") == "proxy"
    await redis.aclose()


async def test_metadata_direct_success(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    state = DomainState(redis, settings)
    calls: list[str | None] = []

    async def fake_direct(url: str, timeout: int) -> bool:
        del url, timeout
        return True

    async def fake_probe(url: str, proxy: str | None, *args: Any, **kwargs: Any) -> MediaMetadata:
        del url, args, kwargs
        calls.append(proxy)
        return _metadata()

    monkeypatch.setattr(netcheck, "direct_available", fake_direct)
    monkeypatch.setattr(netcheck, "_probe", fake_probe)
    await netcheck.fetch_metadata_with_fallback(
        "https://example.com/video", "example.com", settings, state
    )
    assert calls == [None]
    assert await state.get_mode("example.com") == "direct"
    await redis.aclose()


async def test_metadata_retryable_direct_failure(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    state = DomainState(redis, settings)
    calls: list[str | None] = []

    async def fake_direct(url: str, timeout: int) -> bool:
        del url, timeout
        return True

    async def fake_probe(url: str, proxy: str | None, *args: Any, **kwargs: Any) -> MediaMetadata:
        del url, args, kwargs
        calls.append(proxy)
        if proxy is None:
            raise RuntimeError("connection reset")
        return _metadata()

    monkeypatch.setattr(netcheck, "direct_available", fake_direct)
    monkeypatch.setattr(netcheck, "_probe", fake_probe)
    await netcheck.fetch_metadata_with_fallback(
        "https://example.com/video", "example.com", settings, state
    )
    assert calls == [None, settings.proxy_url]
    assert await state.get_mode("example.com") == "proxy"
    await redis.aclose()


async def test_metadata_non_retryable_direct_failure(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    state = DomainState(redis, settings)

    async def fake_direct(url: str, timeout: int) -> bool:
        del url, timeout
        return True

    async def fake_probe(url: str, proxy: str | None, *args: Any, **kwargs: Any) -> MediaMetadata:
        del url, proxy, args, kwargs
        raise RuntimeError("unsupported url")

    monkeypatch.setattr(netcheck, "direct_available", fake_direct)
    monkeypatch.setattr(netcheck, "_probe", fake_probe)
    with pytest.raises(DownloadFailedError):
        await netcheck.fetch_metadata_with_fallback(
            "https://example.com/video", "example.com", settings, state
        )
    await redis.aclose()


async def test_proxy_mode_failure_and_unsupported(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    state = DomainState(redis, settings)
    await state.set("example.com", "proxy", "blocked")

    async def fail_probe(*args: Any, **kwargs: Any) -> MediaMetadata:
        raise RuntimeError("proxy down")

    monkeypatch.setattr(netcheck, "_probe", fail_probe)
    with pytest.raises(DownloadFailedError):
        await netcheck.fetch_metadata_with_fallback(
            "https://example.com/video", "example.com", settings, state
        )

    async def unsupported_probe(*args: Any, **kwargs: Any) -> MediaMetadata:
        from media_loader.errors import UnsupportedMediaError

        raise UnsupportedMediaError("playlist")

    monkeypatch.setattr(netcheck, "_probe", unsupported_probe)
    with pytest.raises(Exception, match="playlist"):
        await netcheck.fetch_metadata_with_fallback(
            "https://example.com/video", "example.com", settings, state
        )
    await redis.aclose()


async def test_direct_unsupported_and_probe_helper(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    state = DomainState(redis, settings)

    async def fake_direct(url: str, timeout: int) -> bool:
        del url, timeout
        return True

    async def unsupported_probe(*args: Any, **kwargs: Any) -> MediaMetadata:
        from media_loader.errors import UnsupportedMediaError

        raise UnsupportedMediaError("live")

    monkeypatch.setattr(netcheck, "direct_available", fake_direct)
    monkeypatch.setattr(netcheck, "_probe", unsupported_probe)
    with pytest.raises(Exception, match="live"):
        await netcheck.fetch_metadata_with_fallback(
            "https://example.com/video", "example.com", settings, state
        )

    await redis.aclose()


async def test_probe_helper(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_probe_source(*args: Any, **kwargs: Any) -> tuple[MediaMetadata, dict[str, Any]]:
        del args, kwargs
        return _metadata(), {"title": "Example"}

    monkeypatch.setattr(netcheck, "probe_source_metadata", fake_probe_source)
    metadata = await netcheck._probe("https://example.com/video", None, settings, True)
    assert metadata.title == "Example"
