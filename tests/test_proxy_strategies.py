from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import fakeredis
import httpx
import pytest

from media_loader import proxy_strategies
from media_loader.config import Settings
from media_loader.proxy_strategies import (
    ProxyStrategyStore,
    _media_urls,
    _probe_info,
    ensure_proxy_strategy,
    is_proxy_strategy_error,
    load_proxy_strategies,
    probe_proxy_strategies,
)
from media_loader.ytdlp import VIDEO_FORMAT_SELECTOR


def test_load_proxy_strategies_from_file(tmp_path: Path, settings: Settings) -> None:
    path = tmp_path / "strategies.conf"
    path.write_text(
        "# comment\n\ntlsrec1 --tlsrec 1+s\ndisorder1 --disorder 1\n",
        encoding="utf-8",
    )
    settings.byedpi_strategies_file = path
    strategies = load_proxy_strategies(settings)
    assert [item.name for item in strategies] == ["tlsrec1", "disorder1"]
    assert [item.proxy_url for item in strategies] == [
        "socks5://127.0.0.1:1080",
        "socks5://127.0.0.1:1081",
    ]
    assert strategies[1].args == "--disorder 1"


def test_load_proxy_strategies_fallback(tmp_path: Path, settings: Settings) -> None:
    settings.byedpi_strategies_file = tmp_path / "missing.conf"
    strategies = load_proxy_strategies(settings)
    assert len(strategies) == 1
    assert strategies[0].name == "default"
    assert strategies[0].proxy_url == settings.proxy_url


def test_media_urls_prefers_requested_direct_formats() -> None:
    info = {
        "formats": [
            {"url": "https://media.example/low", "protocol": "https", "height": 360},
            {"url": "https://media.example/hls", "protocol": "m3u8_native", "height": 1080},
        ],
        "requested_formats": [
            {"url": "https://media.example/video", "protocol": "https"},
            {"url": "https://media.example/audio", "protocol": "https"},
        ],
    }
    assert _media_urls(info) == [
        "https://media.example/video",
        "https://media.example/audio",
    ]


async def test_strategy_store_and_lock(settings: Settings) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = ProxyStrategyStore(redis, settings)
    record = {
        "host": "youtube.com",
        "strategy": "tlsrec1",
        "media_host": "rr1.googlevideo.com",
        "proxy_url": "socks5://byedpi:1080",
        "player_client": "tv_embedded",
    }
    await store.set("youtube.com", record)
    saved = await store.get("youtube.com")
    assert saved is not None
    assert saved["strategy"] == "tlsrec1"
    assert (await store.list())[0]["strategy"] == "tlsrec1"

    token = await store.acquire_lock("youtube.com")
    assert token is not None
    assert await store.acquire_lock("youtube.com") is None
    await store.release_lock("youtube.com", "wrong")
    assert await store.acquire_lock("youtube.com") is None
    await store.release_lock("youtube.com", token)
    assert await store.acquire_lock("youtube.com") is not None
    await store.delete("youtube.com")
    assert await store.get("youtube.com") is None
    await redis.aclose()


def test_probe_proxy_strategies_skips_failed_candidate(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "strategies.conf"
    path.write_text("first --first\nsecond --second\n", encoding="utf-8")
    settings.byedpi_strategies_file = path
    settings.ytdlp_youtube_player_clients = "tv_embedded"
    calls: list[str] = []

    def fake_probe(
        url: str,
        proxy: str,
        settings: Settings,
        *,
        player_client: str | None = None,
        format_selector: str | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        del url, settings, format_selector
        calls.append(str(player_client))
        return object(), {
            "requested_formats": [{"url": "https://media.example/v", "protocol": "https"}],
        }

    def fake_media_probe(url: str, proxy: str, settings: Settings) -> bool:
        del url, settings
        return proxy.endswith(":1081")

    monkeypatch.setattr(proxy_strategies, "probe_source_metadata", fake_probe)
    monkeypatch.setattr(proxy_strategies, "_probe_media_url", fake_media_probe)
    record = probe_proxy_strategies("https://www.youtube.com/watch?v=x", "youtube.com", settings)
    assert record["strategy"] == "second"
    assert record["player_client"] == "tv_embedded"
    assert record["media_host"] == "media.example"
    assert calls == ["tv_embedded", "tv_embedded"]


async def test_ensure_proxy_strategy_uses_lock(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = ProxyStrategyStore(redis, settings)
    record = {
        "host": "youtube.com",
        "strategy": "tlsrec1",
        "media_host": "rr1.googlevideo.com",
        "proxy_url": "socks5://byedpi:1080",
        "player_client": "tv_embedded",
    }
    monkeypatch.setattr(
        proxy_strategies,
        "probe_proxy_strategies",
        lambda *args: record,
    )
    result = await ensure_proxy_strategy(
        "https://www.youtube.com/watch?v=x",
        "youtube.com",
        settings,
        store,
    )
    assert result["strategy"] == "tlsrec1"
    assert await store.get("youtube.com") == json.loads(json.dumps(record))
    assert await redis.exists("ml:proxy:probe:youtube.com") == 0
    await redis.aclose()


def test_proxy_strategy_error_markers() -> None:
    assert is_proxy_strategy_error("Connection reset by peer") is True
    assert is_proxy_strategy_error("read timed out") is True
    assert is_proxy_strategy_error("The downloaded file is empty") is True
    assert is_proxy_strategy_error("HTTP Error 403: Forbidden") is False
    assert is_proxy_strategy_error("permission denied") is False


def test_load_proxy_strategies_warns_on_port_mismatch(
    tmp_path: Path,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "strategies.conf"
    path.write_text("tlsrec1 --tlsrec 1+s\n", encoding="utf-8")
    settings.byedpi_strategies_file = path
    settings.byedpi_base_port = 1090
    with caplog.at_level("WARNING"):
        strategies = load_proxy_strategies(settings)
    assert strategies[0].proxy_url == "socks5://127.0.0.1:1090"
    assert "does not match" in caplog.text


def test_probe_media_url_handles_transport_error(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise OSError("no network")

    monkeypatch.setattr(httpx, "Client", BrokenClient)
    assert (
        proxy_strategies._probe_media_url("https://media.example/v", "socks5://proxy", settings)
        is False
    )


def test_probe_info_falls_back_to_bestaudio(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selectors: list[str | None] = []

    def fake_probe(
        url: str,
        proxy: str,
        settings: Settings,
        *,
        player_client: str | None = None,
        format_selector: str | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        del url, proxy, settings, player_client
        selectors.append(format_selector)
        if format_selector == VIDEO_FORMAT_SELECTOR:
            raise RuntimeError("requested video format is not available")
        return object(), {
            "requested_formats": [{"url": "https://media.example/audio", "protocol": "https"}]
        }

    monkeypatch.setattr(proxy_strategies, "probe_source_metadata", fake_probe)
    info = _probe_info("https://example.com/audio", "socks5://proxy", settings, None)
    assert info["requested_formats"][0]["url"] == "https://media.example/audio"
    assert selectors == [VIDEO_FORMAT_SELECTOR, "bestaudio/best"]
