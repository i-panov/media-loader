from __future__ import annotations

import asyncio
import logging
import ssl
from urllib.parse import urlsplit

from media_loader.config import Settings
from media_loader.domain_state import DomainState
from media_loader.errors import DownloadFailedError, UnsupportedMediaError
from media_loader.proxy_strategies import ProxyStrategyStore, ensure_proxy_strategy
from media_loader.ytdlp import MediaMetadata, is_retryable_error, probe_source_metadata

LOGGER = logging.getLogger(__name__)


async def direct_available(url: str, timeout: int) -> bool:
    parsed = urlsplit(url)
    host = parsed.hostname
    if not host:
        return False
    secure = parsed.scheme.lower() == "https"
    port = parsed.port or (443 if secure else 80)
    context = ssl.create_default_context() if secure else None
    writer = None
    try:
        async with asyncio.timeout(timeout):
            _reader, writer = await asyncio.open_connection(
                host,
                port,
                ssl=context,
                server_hostname=host if secure else None,
            )
            writer.close()
            await writer.wait_closed()
        return True
    except Exception:
        return False
    finally:
        if writer is not None:
            writer.close()


async def _probe(
    canonical_url: str,
    proxy: str | None,
    settings: Settings,
    fast_fail: bool,
    player_client: str | None = None,
) -> MediaMetadata:
    metadata, _info = await asyncio.to_thread(
        probe_source_metadata,
        canonical_url,
        proxy,
        settings,
        fast_fail,
        player_client=player_client,
    )
    return metadata


async def _probe_proxy(
    canonical_url: str,
    host: str,
    settings: Settings,
    proxy_strategies: ProxyStrategyStore | None,
) -> MediaMetadata:
    strategy = await proxy_strategies.get(host) if proxy_strategies else None
    player_client = (
        str(strategy.get("player_client")) if strategy and strategy.get("player_client") else None
    )
    if strategy:
        try:
            return await _probe(
                canonical_url,
                str(strategy["proxy_url"]),
                settings,
                False,
                player_client=player_client,
            )
        except UnsupportedMediaError:
            raise
        except Exception as exc:
            # A strategy can be stale. Keep it for the downloader, but try the
            # default proxy so a metadata-only request does not fail early.
            LOGGER.debug(
                "saved proxy strategy %s failed for metadata on %s: %s; falling back to base proxy",
                strategy.get("strategy"),
                host,
                exc,
            )
    try:
        return await _probe(
            canonical_url,
            settings.proxy_url,
            settings,
            False,
            player_client=player_client,
        )
    except UnsupportedMediaError:
        raise
    except Exception as base_exc:
        if strategy is not None or proxy_strategies is None or not is_retryable_error(base_exc):
            raise
        LOGGER.debug(
            "base proxy metadata failed for %s: %s; probing strategies",
            host,
            base_exc,
        )
        selected = await ensure_proxy_strategy(
            canonical_url,
            host,
            settings,
            proxy_strategies,
        )
        selected_client = (
            str(selected.get("player_client")) if selected.get("player_client") else None
        )
        return await _probe(
            canonical_url,
            str(selected["proxy_url"]),
            settings,
            False,
            player_client=selected_client,
        )


async def fetch_metadata_with_fallback(
    canonical_url: str,
    host: str,
    settings: Settings,
    domain_state: DomainState,
    proxy_strategies: ProxyStrategyStore | None = None,
) -> MediaMetadata:
    mode = await domain_state.get_mode(host)
    if mode == "proxy":
        try:
            return await _probe_proxy(
                canonical_url,
                host,
                settings,
                proxy_strategies,
            )
        except UnsupportedMediaError:
            raise
        except Exception as exc:
            raise DownloadFailedError(str(exc)) from exc

    if mode is None and not await direct_available(
        canonical_url,
        settings.direct_probe_timeout,
    ):
        await domain_state.set(host, "proxy", "direct TLS probe failed")
        try:
            return await _probe_proxy(
                canonical_url,
                host,
                settings,
                proxy_strategies,
            )
        except UnsupportedMediaError:
            raise
        except Exception as exc:
            raise DownloadFailedError(str(exc)) from exc

    try:
        metadata = await _probe(canonical_url, None, settings, True)
        await domain_state.set(host, "direct", "direct metadata succeeded")
        return metadata
    except UnsupportedMediaError:
        raise
    except Exception as exc:
        if not is_retryable_error(exc):
            raise DownloadFailedError(str(exc)) from exc
        await domain_state.set(host, "proxy", f"direct metadata failed: {exc}")
        try:
            return await _probe_proxy(
                canonical_url,
                host,
                settings,
                proxy_strategies,
            )
        except UnsupportedMediaError:
            raise
        except Exception as proxy_exc:
            raise DownloadFailedError(str(proxy_exc)) from proxy_exc
