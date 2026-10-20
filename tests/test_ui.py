from __future__ import annotations

from typing import Any, cast

import fakeredis.aioredis
import httpx
import pytest

from media_loader.api import create_app
from media_loader.config import Settings


class FakeArq:
    async def enqueue_job(self, name: str, *args: Any, **kwargs: Any) -> Any:
        del name, args, kwargs
        return object()


async def _ui_client(settings: Settings) -> tuple[Any, httpx.AsyncClient]:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = create_app(
        settings=settings,
        redis_client=redis,
        arq_pool=cast(Any, FakeArq()),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )
    return app, client


async def test_ui_static_and_config(settings: Settings) -> None:
    settings.auth_enabled = True
    app, client = await _ui_client(settings)
    async with app.router.lifespan_context(app):
        index = await client.get("/ui")
        assert index.status_code == 200
        assert 'id="download-form"' in index.text
        assert "/static/app.js" in index.text

        root = await client.get("/")
        assert root.status_code == 200

        script = await client.get("/static/app.js")
        assert script.status_code == 200
        styles = await client.get("/static/styles.css")
        assert styles.status_code == 200

        config = await client.get("/ui/config")
        assert config.status_code == 200
        payload = config.json()
        assert payload["auth_required"] is True
        assert "mp4" in payload["video_formats"]
        assert "mp3" in payload["audio_formats"]
        assert payload["quality_levels"]

        unauthorized = await client.post(
            "/videos",
            json={"url": "https://example.com/video"},
        )
        assert unauthorized.status_code == 401
    await client.aclose()


async def test_ui_config_when_auth_disabled(settings: Settings) -> None:
    app, client = await _ui_client(settings)
    async with app.router.lifespan_context(app):
        config = await client.get("/ui/config")
        assert config.status_code == 200
        assert config.json()["auth_required"] is False
    await client.aclose()


async def test_ui_index_missing_static(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from media_loader import api

    monkeypatch.setattr(api, "STATIC_DIR", settings.data_dir / "missing-static")
    app, client = await _ui_client(settings)
    async with app.router.lifespan_context(app):
        response = await client.get("/ui")
        assert response.status_code == 404
    await client.aclose()
