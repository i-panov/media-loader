from __future__ import annotations

import asyncio
import ssl
from urllib.parse import urlsplit

from media_loader.config import Settings
from media_loader.domain_state import DomainState
from media_loader.errors import DownloadFailedError, UnsupportedMediaError
from media_loader.ytdlp import MediaMetadata, is_retryable_error, probe_source_metadata


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
) -> MediaMetadata:
    metadata, _info = await asyncio.to_thread(
        probe_source_metadata,
        canonical_url,
        proxy,
        settings,
        fast_fail,
    )
    return metadata


async def fetch_metadata_with_fallback(
    canonical_url: str,
    host: str,
    settings: Settings,
    domain_state: DomainState,
) -> MediaMetadata:
    mode = await domain_state.get_mode(host)
    if mode == "proxy":
        try:
            return await _probe(canonical_url, settings.proxy_url, settings, False)
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
            return await _probe(canonical_url, settings.proxy_url, settings, False)
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
            return await _probe(canonical_url, settings.proxy_url, settings, False)
        except UnsupportedMediaError:
            raise
        except Exception as proxy_exc:
            raise DownloadFailedError(str(proxy_exc)) from proxy_exc
