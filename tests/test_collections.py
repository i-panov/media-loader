from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, cast

import fakeredis.aioredis
import httpx
import pytest

import media_loader.api as api
import media_loader.worker as worker
from media_loader.collections import CollectionStore, collection_key
from media_loader.config import Settings
from media_loader.domain_state import DomainState
from media_loader.errors import UnsupportedMediaError
from media_loader.metadata_db import MetadataDatabase
from media_loader.proxy_strategies import ProxyStrategyStore
from media_loader.schemas import ResolvedRequest
from media_loader.series import CollectionSpec, EpisodeRef
from media_loader.store import Store
from media_loader.utils import iso, utcnow


class FakeArq:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def enqueue_job(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((name, args, kwargs))
        return object()


def _spec(count: int = 2) -> CollectionSpec:
    return CollectionSpec(
        series_id="rutube:991",
        title="Test series",
        season=1,
        episodes=tuple(
            EpisodeRef(
                url=f"https://example.com/episode-{index}",
                title=f"Episode {index}",
                season=1,
                episode=index,
                duration=60.0,
                position=index - 1,
            )
            for index in range(1, count + 1)
        ),
    )


def _collection_record(settings: Settings, count: int = 2) -> dict[str, Any]:
    spec = _spec(count)
    key = collection_key(
        spec.series_id,
        "season",
        spec.season,
        "video",
        "mp4",
        "720p",
    )
    return api._new_collection_record(
        key,
        "season",
        spec,
        ResolvedRequest("video", "mp4", "720p"),
        "https://example.com/series",
    )


def _worker_ctx(
    settings: Settings,
    redis: fakeredis.aioredis.FakeRedis,
    collections: CollectionStore,
) -> dict[str, Any]:
    return {
        "settings": settings,
        "store": Store(redis, settings),
        "domain_state": DomainState(redis, settings),
        "collections": collections,
        "metadata_db": MetadataDatabase(settings.metadata_db_path),
        "transcode_sem": asyncio.Semaphore(1),
        "proxy_strategies": ProxyStrategyStore(redis, settings),
    }


async def _fake_ready(
    settings: Settings,
    store: Store,
    job: dict[str, Any],
) -> None:
    path = store.job_dir(str(job["request_key"])) / f"episode-{job['episode']}.mp4"
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, b"episode")
    job.update(
        status="ready",
        stage="ready",
        output_path=str(path),
        output_filename=path.name,
        size=path.stat().st_size,
        title=f"Episode {job['episode']}",
        duration=60.0,
        actual_quality="720p",
        content_type="video/mp4",
        finished_at=iso(utcnow()),
        expires_at=iso(utcnow() + timedelta(seconds=settings.collection_ttl)),
    )
    await store.save_job(job, ttl=settings.collection_ttl)


def test_collection_key_depends_on_quality() -> None:
    first = collection_key("series", "season", 1, "video", "mp4", "720p")
    second = collection_key("series", "season", 1, "video", "mp4", "1080p")
    third = collection_key("series", "season", 1, "video", "mp4", "720p")
    assert first == third
    assert first != second


async def test_process_collection_marks_episodes_ready(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    collections = CollectionStore(redis, settings)
    ctx = _worker_ctx(settings, redis, collections)
    record = _collection_record(settings)
    assert await collections.create_nx(record)
    seen: list[int] = []

    async def fake_process(
        settings: Settings,
        store: Store,
        domain_state: DomainState,
        metadata_db: MetadataDatabase,
        transcode_sem: asyncio.Semaphore,
        proxy_strategies: ProxyStrategyStore,
        job: dict[str, Any],
    ) -> None:
        del domain_state, metadata_db, transcode_sem, proxy_strategies
        seen.append(int(job["episode"]))
        await _fake_ready(settings, store, job)

    monkeypatch.setattr(worker, "_process_job", fake_process)
    await worker.process_collection(ctx, record["collection_key"])

    saved = await collections.get(record["collection_key"])
    assert saved is not None
    assert saved["status"] == "ready"
    assert saved["episodes_ready"] == 2
    assert saved["episodes_failed"] == 0
    assert saved["progress"]["percent"] == 100.0
    assert [episode["status"] for episode in saved["episodes"]] == ["ready", "ready"]
    assert seen == [1, 2]
    assert saved["expires_at"]
    await redis.aclose()


async def test_process_collection_keeps_partial_success(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    collections = CollectionStore(redis, settings)
    ctx = _worker_ctx(settings, redis, collections)
    record = _collection_record(settings)
    assert await collections.create_nx(record)

    async def fake_process(
        settings: Settings,
        store: Store,
        domain_state: DomainState,
        metadata_db: MetadataDatabase,
        transcode_sem: asyncio.Semaphore,
        proxy_strategies: ProxyStrategyStore,
        job: dict[str, Any],
    ) -> None:
        del domain_state, metadata_db, transcode_sem, proxy_strategies
        if job["episode"] == 2:
            job.update(status="failed", stage="failed", error="boom")
            await store.save_job(job, ttl=settings.collection_ttl)
            return
        await _fake_ready(settings, store, job)

    monkeypatch.setattr(worker, "_process_job", fake_process)
    await worker.process_collection(ctx, record["collection_key"])

    saved = await collections.get(record["collection_key"])
    assert saved is not None
    assert saved["status"] == "ready"
    assert saved["partial"] is True
    assert saved["episodes_ready"] == 1
    assert saved["episodes_failed"] == 1
    assert saved["episodes"][1]["error"] == "boom"
    await redis.aclose()


async def test_process_collection_fails_when_all_episodes_fail(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    collections = CollectionStore(redis, settings)
    ctx = _worker_ctx(settings, redis, collections)
    record = _collection_record(settings)
    assert await collections.create_nx(record)

    async def fake_process(
        settings: Settings,
        store: Store,
        domain_state: DomainState,
        metadata_db: MetadataDatabase,
        transcode_sem: asyncio.Semaphore,
        proxy_strategies: ProxyStrategyStore,
        job: dict[str, Any],
    ) -> None:
        del domain_state, metadata_db, transcode_sem, proxy_strategies
        job.update(status="failed", stage="failed", error="boom")
        await store.save_job(job, ttl=settings.collection_ttl)

    monkeypatch.setattr(worker, "_process_job", fake_process)
    await worker.process_collection(ctx, record["collection_key"])

    saved = await collections.get(record["collection_key"])
    assert saved is not None
    assert saved["status"] == "failed"
    assert saved["error_code"] == "collection_failed"
    await redis.aclose()


async def test_process_collection_reuses_ready_episode(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    collections = CollectionStore(redis, settings)
    ctx = _worker_ctx(settings, redis, collections)
    record = _collection_record(settings, count=1)
    assert await collections.create_nx(record)
    store: Store = ctx["store"]
    episode = record["episodes"][0]
    job_dir = store.job_dir(episode["request_key"])
    job_dir.mkdir(parents=True, exist_ok=True)
    output_path = job_dir / "old.mp4"
    output_path.write_bytes(b"episode")
    job = {
        "request_key": episode["request_key"],
        "source_key": episode["source_key"],
        "url": episode["url"],
        "canonical_url": episode["canonical_url"],
        "media_type": "video",
        "format": "mp4",
        "quality": "720p",
        "season": 1,
        "episode": 1,
        "status": "ready",
        "stage": "ready",
        "output_path": str(output_path),
        "output_filename": "old.mp4",
        "title": "Old episode",
        "size": 7,
        "duration": 60.0,
        "actual_quality": "720p",
        "content_type": "video/mp4",
    }
    await store.save_job(job, ttl=60)

    async def fail_process(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("ready episode must be reused")

    monkeypatch.setattr(worker, "_process_job", fail_process)
    await worker.process_collection(ctx, record["collection_key"])

    saved = await collections.get(record["collection_key"])
    assert saved is not None
    assert saved["status"] == "ready"
    assert saved["episodes"][0]["title"] == "Old episode"
    assert saved["episodes_ready"] == 1
    await redis.aclose()


def test_title_for_output_prefixes_episode() -> None:
    name = worker._title_for_output("Episode", "video", "720p", "mp4", 1, 2)
    assert name.startswith("S01E02 - Episode [720p]")


async def _api_client(
    settings: Settings,
    redis: Any,
    arq: FakeArq,
) -> tuple[Any, httpx.AsyncClient]:
    app = api.create_app(settings=settings, redis_client=redis, arq_pool=cast(Any, arq))
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return app, client


async def test_api_collection_returns_manifest_with_episodes(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "resolve_collection", lambda *args: _spec())
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    arq = FakeArq()
    app, client = await _api_client(settings, redis, arq)
    try:
        async with app.router.lifespan_context(app):
            response = await client.post(
                "/videos",
                json={
                    "url": "https://example.com/series",
                    "scope": "season",
                    "format": "mp4",
                    "quality": "720p",
                },
            )
            assert response.status_code == 202
            payload = response.json()
            assert payload["collection"] is True
            assert payload["status"] == "queued"
            assert payload["episodes_total"] == 2
            assert arq.calls[0][0] == "process_collection"
            collection_key_value = str(arq.calls[0][1][0])
            collections = app.state.collections
            collection = await collections.get(collection_key_value)
            assert collection is not None

            store: Store = app.state.store
            for index, episode in enumerate(collection["episodes"], start=1):
                path = store.job_dir(episode["request_key"]) / f"S01E0{index} - Ep.mp4"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"episode")
                job = {
                    "request_key": episode["request_key"],
                    "source_key": episode["source_key"],
                    "url": episode["url"],
                    "canonical_url": episode["canonical_url"],
                    "media_type": "video",
                    "format": "mp4",
                    "quality": "720p",
                    "season": 1,
                    "episode": index,
                    "status": "ready",
                    "stage": "ready",
                    "output_path": str(path),
                    "output_filename": path.name,
                    "size": path.stat().st_size,
                    "duration": 60.0,
                    "title": f"Episode {index}",
                    "actual_quality": "720p",
                    "content_type": "video/mp4",
                    "finished_at": iso(utcnow()),
                    "ttl": settings.collection_ttl,
                }
                await store.save_job(job, ttl=settings.collection_ttl)
                episode["status"] = "ready"
            collection.update(
                status="ready",
                stage="ready",
                episodes_ready=2,
                episodes_failed=0,
                finished_at=iso(utcnow()),
            )
            await collections.save(collection)

            response = await client.post(
                "/videos",
                json={
                    "url": "https://example.com/series",
                    "scope": "season",
                    "format": "mp4",
                    "quality": "720p",
                },
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["status"] == "ready"
            assert payload["partial"] is False
            assert [episode["episode"] for episode in payload["episodes"]] == [1, 2]
            assert payload["episodes"][0]["download_url"].startswith("/downloads/")
            download = await client.get(payload["episodes"][0]["download_url"])
            assert download.status_code == 200
            assert download.content == b"episode"
            assert download.headers["x-media-season"] == "1"
            assert download.headers["x-media-episode"] == "1"
    finally:
        await client.aclose()
        await redis.aclose()


def test_collection_key_depends_on_selected_episodes() -> None:
    full = collection_key("series", "season", 1, "video", "mp4", "720p")
    selected = collection_key(
        "series",
        "season",
        1,
        "video",
        "mp4",
        "720p",
        episodes=(1, 3),
    )
    assert full != selected
    assert selected == collection_key(
        "series",
        "season",
        1,
        "video",
        "mp4",
        "720p",
        episodes=(3, 1),
    )


async def test_api_collection_filters_selected_episodes(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "resolve_collection", lambda *args: _spec(4))
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    arq = FakeArq()
    app, client = await _api_client(settings, redis, arq)
    try:
        async with app.router.lifespan_context(app):
            response = await client.post(
                "/videos",
                json={
                    "url": "https://example.com/series",
                    "scope": "season",
                    "episodes": [2, 4],
                    "metadata_only": True,
                },
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["episodes_total"] == 2
            assert [episode["episode"] for episode in payload["episodes"]] == [2, 4]
            assert arq.calls == []
    finally:
        await client.aclose()
        await redis.aclose()


async def test_api_collection_rejects_unknown_episode(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "resolve_collection", lambda *args: _spec(2))
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    arq = FakeArq()
    app, client = await _api_client(settings, redis, arq)
    try:
        async with app.router.lifespan_context(app):
            response = await client.post(
                "/videos",
                json={
                    "url": "https://example.com/series",
                    "scope": "season",
                    "episodes": [99],
                },
            )
            assert response.status_code == 422
            assert "unknown episode" in response.json()["detail"]
            assert arq.calls == []
    finally:
        await client.aclose()
        await redis.aclose()


async def test_api_collection_rejects_bad_episode_selection(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "resolve_collection", lambda *args: _spec(2))
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    arq = FakeArq()
    app, client = await _api_client(settings, redis, arq)
    try:
        async with app.router.lifespan_context(app):
            empty = await client.post(
                "/videos",
                json={
                    "url": "https://example.com/series",
                    "scope": "season",
                    "episodes": [],
                },
            )
            assert empty.status_code == 422
            assert "must not be empty" in empty.json()["detail"]

            series = await client.post(
                "/videos",
                json={
                    "url": "https://example.com/series",
                    "scope": "series",
                    "episodes": [1],
                },
            )
            assert series.status_code == 422
            assert "scope=season" in series.json()["detail"]
    finally:
        await client.aclose()
        await redis.aclose()


async def test_api_auto_scope_uses_detected_season(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "detect_series_scope", lambda *args: "season")
    monkeypatch.setattr(api, "resolve_collection", lambda *args: _spec(2))
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    arq = FakeArq()
    app, client = await _api_client(settings, redis, arq)
    try:
        async with app.router.lifespan_context(app):
            response = await client.post(
                "/videos",
                json={
                    "url": "https://example.com/series",
                    "scope": "auto",
                    "metadata_only": True,
                },
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["collection"] is True
            assert payload["scope"] == "season"
            assert payload["episodes_total"] == 2
    finally:
        await client.aclose()
        await redis.aclose()


async def test_api_collection_metadata_only_does_not_enqueue(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "resolve_collection", lambda *args: _spec())
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    arq = FakeArq()
    app, client = await _api_client(settings, redis, arq)
    try:
        async with app.router.lifespan_context(app):
            response = await client.post(
                "/videos",
                json={
                    "url": "https://example.com/series",
                    "scope": "series",
                    "metadata_only": True,
                },
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["metadata_only"] is True
            assert payload["episodes_total"] == 2
            assert payload["episodes"][0]["episode"] == 1
            assert arq.calls == []
    finally:
        await client.aclose()
        await redis.aclose()


async def test_api_collection_enforces_episode_limit(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.max_collection_episodes = 1
    monkeypatch.setattr(api, "resolve_collection", lambda *args: _spec())
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    arq = FakeArq()
    app, client = await _api_client(settings, redis, arq)
    try:
        async with app.router.lifespan_context(app):
            response = await client.post(
                "/videos",
                json={"url": "https://example.com/series", "scope": "season"},
            )
            assert response.status_code == 422
            assert "MAX_COLLECTION_EPISODES" in response.json()["detail"]
            assert arq.calls == []
    finally:
        await client.aclose()
        await redis.aclose()


async def test_api_collection_maps_resolution_error(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: Any) -> CollectionSpec:
        raise UnsupportedMediaError("not a series")

    monkeypatch.setattr(api, "resolve_collection", fail)
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    arq = FakeArq()
    app, client = await _api_client(settings, redis, arq)
    try:
        async with app.router.lifespan_context(app):
            response = await client.post(
                "/videos",
                json={"url": "https://example.com/series", "scope": "season"},
            )
            assert response.status_code == 422
            assert response.json()["detail"] == "not a series"
    finally:
        await client.aclose()
        await redis.aclose()


async def test_recover_incomplete_collection_marks_failed(settings: Settings) -> None:
    redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
    collections = CollectionStore(redis, settings)
    store = Store(redis, settings)
    record = _collection_record(settings)
    assert await collections.create_nx(record)

    recovered = await collections.recover_incomplete(store)

    saved = await collections.get(record["collection_key"])
    assert recovered == 1
    assert saved is not None
    assert saved["status"] == "failed"
    assert saved["error_code"] == "worker_restarted"
    await redis.aclose()
