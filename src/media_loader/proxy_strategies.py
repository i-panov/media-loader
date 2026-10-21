from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
import redis.asyncio as aioredis

from media_loader.canonical import is_youtube_url
from media_loader.config import Settings
from media_loader.errors import ProxyProbeError
from media_loader.utils import iso, utcnow
from media_loader.ytdlp import VIDEO_FORMAT_SELECTOR, probe_source_metadata

LOGGER = logging.getLogger(__name__)

STRATEGY_PREFIX = "ml:proxy:strategy:"
PROBE_LOCK_PREFIX = "ml:proxy:probe:"
_PROXY_FAILURE_MARKERS = (
    "connection reset",
    "connection aborted",
    "connection refused",
    "read timed out",
    "timed out",
    "timeout",
    "no bytes",
    "downloaded file is empty",
    "file is empty",
    "remote end closed",
    "broken pipe",
    "eof occurred",
    "network is unreachable",
    "name or service not known",
    "temporary failure in name resolution",
    "socks",
)


@dataclass(frozen=True)
class ProxyStrategy:
    name: str
    proxy_url: str
    args: str


def is_proxy_strategy_error(error: BaseException | str) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in _PROXY_FAILURE_MARKERS)


def strategy_key(host: str) -> str:
    return f"{STRATEGY_PREFIX}{host}"


def probe_lock_key(host: str) -> str:
    return f"{PROBE_LOCK_PREFIX}{host}"


def _proxy_url(settings: Settings, port: int) -> str:
    parsed = urlsplit(settings.proxy_url)
    scheme = parsed.scheme or "socks5"
    host = parsed.hostname or "byedpi"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{scheme}://{host}:{port}"


def load_proxy_strategies(settings: Settings) -> tuple[ProxyStrategy, ...]:
    path = settings.byedpi_strategies_file
    parsed = urlsplit(settings.proxy_url)
    proxy_port = parsed.port or 1080
    base_port = settings.byedpi_base_port
    if proxy_port != base_port:
        LOGGER.warning(
            "PROXY_URL port %s does not match BYEDPI_BASE_PORT %s; "
            "metadata and strategy probes will use different ports",
            proxy_port,
            base_port,
        )
    strategies: list[ProxyStrategy] = []
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            LOGGER.warning("cannot read proxy strategies file %s: %s", path, exc)
            lines = []
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            name = parts[0]
            args = parts[1] if len(parts) > 1 else ""
            strategies.append(
                ProxyStrategy(
                    name=name,
                    proxy_url=_proxy_url(settings, base_port + len(strategies)),
                    args=args,
                )
            )
    if not strategies:
        strategies.append(ProxyStrategy("default", settings.proxy_url, ""))
    return tuple(strategies)


def _is_direct_media(item: dict[str, Any]) -> bool:
    url = str(item.get("url") or "")
    protocol = str(item.get("protocol") or "")
    if not url.startswith(("http://", "https://")):
        return False
    return "m3u8" not in protocol.lower() and ".m3u8" not in url.lower()


def _media_urls(info: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    requested = info.get("requested_formats") or []
    if isinstance(requested, list):
        urls.extend(
            str(item["url"])
            for item in requested
            if isinstance(item, dict) and _is_direct_media(item)
        )
    if not urls and _is_direct_media(info):
        urls.append(str(info["url"]))
    if not urls:
        formats = [
            item
            for item in info.get("formats") or []
            if isinstance(item, dict) and _is_direct_media(item)
        ]
        formats.sort(
            key=lambda item: (
                int(item.get("height") or 0),
                float(item.get("tbr") or 0),
            ),
            reverse=True,
        )
        if formats:
            urls.append(str(formats[0]["url"]))
    return list(dict.fromkeys(urls))


def _probe_media_url(url: str, proxy_url: str, settings: Settings) -> bool:
    sample_size = max(1, settings.proxy_probe_sample_bytes)
    minimum = min(sample_size, 64 * 1024)
    timeout = httpx.Timeout(settings.proxy_probe_timeout)
    try:
        with (
            httpx.Client(
                proxy=proxy_url,
                timeout=timeout,
                follow_redirects=True,
                trust_env=False,
            ) as client,
            client.stream(
                "GET",
                url,
                headers={"Range": f"bytes=0-{sample_size - 1}"},
            ) as response,
        ):
            if response.status_code not in {200, 206}:
                return False
            content_type = response.headers.get("content-type", "").lower()
            if "mpegurl" in content_type or content_type.startswith("text/"):
                return False
            received = 0
            for chunk in response.iter_bytes():
                received += len(chunk)
                if received >= minimum:
                    return True
            return received >= minimum
    except (httpx.HTTPError, OSError) as exc:
        LOGGER.debug("media probe failed for %s via %s: %s", url, proxy_url, exc)
        return False


def _probe_info(
    url: str,
    proxy_url: str,
    settings: Settings,
    player_client: str | None,
) -> dict[str, Any]:
    last_error: BaseException | None = None
    for selector in (VIDEO_FORMAT_SELECTOR, "bestaudio/best"):
        try:
            _metadata, info = probe_source_metadata(
                url,
                proxy_url,
                settings,
                player_client=player_client,
                format_selector=selector,
            )
            if _media_urls(info):
                return info
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise ProxyProbeError("media probe returned no playable URL")


def probe_proxy_strategies(url: str, host: str, settings: Settings) -> dict[str, Any]:
    strategies = load_proxy_strategies(settings)
    clients: tuple[str | None, ...] = (
        settings.youtube_player_clients if is_youtube_url(url) else (None,)
    )
    deadline = time.monotonic() + settings.proxy_probe_total_timeout
    attempts: list[str] = []
    for strategy in strategies:
        for player_client in clients:
            if time.monotonic() >= deadline:
                attempts.append("total probe timeout")
                break
            label = strategy.name
            if player_client:
                label = f"{label}/{player_client}"
            try:
                info = _probe_info(url, strategy.proxy_url, settings, player_client)
                urls = _media_urls(info)
                if not urls:
                    raise ProxyProbeError("no direct media URL")
                for media_url in urls:
                    if not _probe_media_url(media_url, strategy.proxy_url, settings):
                        raise ProxyProbeError(f"no bytes from {urlsplit(media_url).hostname}")
                media_host = urlsplit(urls[0]).hostname
                now = utcnow()
                record = {
                    "host": host,
                    "media_host": media_host,
                    "strategy": strategy.name,
                    "proxy_url": strategy.proxy_url,
                    "player_client": player_client,
                    "checked_at": iso(now),
                    "expires_at": iso(now + timedelta(seconds=settings.proxy_strategy_ttl)),
                }
                LOGGER.info("selected proxy strategy %s for %s", label, host)
                return record
            except Exception as exc:
                attempts.append(f"{label}: {exc}")
                LOGGER.warning("proxy strategy %s failed for %s: %s", label, host, exc)
        if time.monotonic() >= deadline:
            break
    detail = "; ".join(attempts[-8:])
    raise ProxyProbeError(f"no working proxy strategy for {host}: {detail}")


class ProxyStrategyStore:
    def __init__(self, redis: aioredis.Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings

    async def get(self, host: str) -> dict[str, Any] | None:
        raw = await self.redis.get(strategy_key(host))
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        return cast(dict[str, Any], value) if isinstance(value, dict) else None

    async def set(self, host: str, record: dict[str, Any]) -> None:
        await self.redis.set(
            strategy_key(host),
            json.dumps(record, ensure_ascii=False),
            ex=self.settings.proxy_strategy_ttl,
        )

    async def delete(self, host: str) -> None:
        await self.redis.delete(strategy_key(host))

    async def list(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        keys = [
            str(key) async for key in self.redis.scan_iter(match=f"{STRATEGY_PREFIX}*", count=100)
        ]
        for key in sorted(keys):
            raw = await self.redis.get(key)
            if not raw:
                continue
            try:
                value = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                result.append(cast(dict[str, Any], value))
        return result

    async def acquire_lock(self, host: str) -> str | None:
        token = secrets.token_hex(16)
        acquired = await self.redis.set(
            probe_lock_key(host),
            token,
            nx=True,
            ex=self.settings.proxy_probe_lock_ttl,
        )
        return token if acquired else None

    async def release_lock(self, host: str, token: str) -> None:
        key = probe_lock_key(host)
        current = await self.redis.get(key)
        if current == token:
            await self.redis.delete(key)


async def ensure_proxy_strategy(
    url: str,
    host: str,
    settings: Settings,
    store: ProxyStrategyStore,
) -> dict[str, Any]:
    saved = await store.get(host)
    if saved:
        return saved
    token = await store.acquire_lock(host)
    if token is None:
        deadline = time.monotonic() + settings.proxy_probe_lock_wait
        while time.monotonic() < deadline:
            saved = await store.get(host)
            if saved:
                return saved
            await asyncio.sleep(1)
        raise ProxyProbeError(f"proxy strategy probe is already running for {host}")
    try:
        saved = await store.get(host)
        if saved:
            return saved
        record = await asyncio.to_thread(probe_proxy_strategies, url, host, settings)
        await store.set(host, record)
        return record
    finally:
        await store.release_lock(host, token)
