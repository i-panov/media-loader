from __future__ import annotations

import json
from typing import Any

import pytest

import media_loader.series as series
from media_loader.config import Settings
from media_loader.errors import UnsupportedMediaError
from media_loader.series import EpisodeRef, spec_from_dict, spec_to_dict


class _Response:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    def __init__(
        self,
        pages: dict[str, Any],
        search: dict[str, list[dict[str, Any]]] | None = None,
        tv_search: list[dict[str, Any]] | None = None,
    ) -> None:
        self.pages = pages
        self.search = search or {}
        self.tv_search = tv_search or []

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, url: str, params: dict[str, Any] | None = None) -> _Response:
        if params and "search" in params:
            return _Response({"results": self.tv_search, "has_next": False})
        if "/search/video/" in url:
            query = str((params or {}).get("query") or "")
            return _Response({"results": self.search.get(query, []), "has_next": False})
        for fragment, payload in self.pages.items():
            if fragment in url:
                return _Response(payload)
        return _Response({"detail": "not found"}, status_code=404)


def _episode(
    video_id: str,
    season: int,
    episode: int,
    title: str | None = None,
) -> dict[str, Any]:
    return {
        "id": video_id,
        "title": title or f"{episode} серия",
        "video_url": f"https://rutube.ru/video/{video_id}/",
        "season": season,
        "episode": episode,
        "duration": 1200,
    }


@pytest.fixture
def rutube_pages() -> dict[str, Any]:
    return {
        "/api/video/": {"tv_show_id": 891161, "season": 1, "episode": 5},
        "/metainfo/tv/891161/?format=json": {"name": "Универ | PREMIER"},
        "/metainfo/tv/891161/season/": [{"number": 1}, {"number": 2}],
        "season=1": {
            "results": [_episode("a" * 32, 1, 1), _episode("b" * 32, 1, 2)],
            "has_next": False,
        },
        "season=2": {
            "results": [_episode("c" * 32, 2, 1), _episode("d" * 32, 2, 2)],
            "has_next": False,
        },
    }


def test_spec_roundtrip_keeps_order() -> None:
    spec = series.CollectionSpec(
        series_id="generic:show",
        title="Show",
        season=None,
        episodes=(
            EpisodeRef("https://example.com/e1", "One", 1, 1, 10.0, 0),
            EpisodeRef("https://example.com/e2", "Two", 1, 2, 20.0, 1),
        ),
    )
    restored = spec_from_dict(spec_to_dict(spec))
    assert restored.series_id == spec.series_id
    assert restored.title == spec.title
    assert [episode.url for episode in restored.episodes] == [
        "https://example.com/e1",
        "https://example.com/e2",
    ]
    assert restored.episodes[1].episode == 2


def test_vk_auto_detects_episode_title(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(series.httpx, "Client", lambda **kwargs: _FakeClient({}))  # type: ignore[attr-defined]
    monkeypatch.setattr(
        series,
        "_vk_video_title",
        lambda *args: "Полуночный мотив сердца. Сезон 1. Серия 2",
    )
    assert (
        series.detect_series_scope(
            "https://vkvideo.ru/video-238771780_456240685",
            settings,
        )
        == "season"
    )


def test_vk_auto_keeps_standalone_video(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(series.httpx, "Client", lambda **kwargs: _FakeClient({}))  # type: ignore[attr-defined]
    monkeypatch.setattr(series, "_vk_video_title", lambda *args: "Обычный ролик")
    assert (
        series.detect_series_scope(
            "https://vkvideo.ru/video-238771780_456240685",
            settings,
        )
        == "episode"
    )


def test_rutube_season_resolution(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    rutube_pages: dict[str, Any],
) -> None:
    monkeypatch.setattr(series.httpx, "Client", lambda **kwargs: _FakeClient(rutube_pages))  # type: ignore[attr-defined]
    spec = series.resolve_collection(
        "https://rutube.ru/video/" + "e" * 32 + "/",
        "season",
        settings,
    )
    assert spec.series_id == "rutube:891161"
    assert spec.title == "Универ | PREMIER"
    assert spec.season == 1
    assert [(episode.season, episode.episode) for episode in spec.episodes] == [(1, 1), (1, 2)]


def test_rutube_series_resolution_uses_all_seasons(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    rutube_pages: dict[str, Any],
) -> None:
    monkeypatch.setattr(series.httpx, "Client", lambda **kwargs: _FakeClient(rutube_pages))  # type: ignore[attr-defined]
    spec = series.resolve_collection(
        "https://rutube.ru/video/" + "e" * 32 + "/",
        "series",
        settings,
    )
    assert [(episode.season, episode.episode) for episode in spec.episodes] == [
        (1, 1),
        (1, 2),
        (2, 1),
        (2, 2),
    ]


def test_rutube_video_without_series_is_rejected(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        "/api/video/": {"tv_show_id": None, "season": None, "episode": None},
    }
    monkeypatch.setattr(series.httpx, "Client", lambda **kwargs: _FakeClient(pages))  # type: ignore[attr-defined]
    with pytest.raises(UnsupportedMediaError, match="not linked to a series"):
        series.resolve_collection(
            "https://rutube.ru/video/" + "f" * 32 + "/",
            "season",
            settings,
        )


def test_generic_playlist_series(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = {
        "_type": "playlist",
        "id": "show-1",
        "title": "Generic show",
        "entries": [
            {
                "url": "https://example.com/e1",
                "title": "One",
                "season_number": 1,
                "episode_number": 1,
            },
            {
                "url": "https://example.com/e2",
                "title": "Two",
                "season_number": 1,
                "episode_number": 2,
            },
            {
                "url": "https://example.com/e3",
                "title": "Three",
                "season_number": 2,
                "episode_number": 1,
            },
        ],
    }

    class _Ydl:
        def __init__(self, options: Any) -> None:
            self.options = options

        def __enter__(self) -> _Ydl:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            del url, download
            return info

    monkeypatch.setattr(series, "YoutubeDL", _Ydl)
    spec = series.resolve_collection("https://example.com/series", "series", settings)
    assert spec.series_id == "generic:show-1"
    assert [episode.episode for episode in spec.episodes] == [1, 2, 1]
    assert [episode.season for episode in spec.episodes] == [1, 1, 2]


def test_generic_playlist_requires_episode_for_season(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = {
        "_type": "playlist",
        "id": "show-1",
        "title": "Generic show",
        "entries": [
            {"url": "https://example.com/e1", "season_number": 1},
            {"url": "https://example.com/e2", "season_number": 2},
        ],
    }

    class _Ydl:
        def __init__(self, options: Any) -> None:
            del options

        def __enter__(self) -> _Ydl:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            del url, download
            return info

    monkeypatch.setattr(series, "YoutubeDL", _Ydl)
    with pytest.raises(UnsupportedMediaError, match="cannot determine the season"):
        series.resolve_collection("https://example.com/series", "season", settings)


def test_rutube_playlist_id_fallback(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        "/api/video/": {"tv_show_id": None, "season": None, "episode": None},
        "/metainfo/tv/1454763/?format=json": {"name": "Playlist series"},
        "/metainfo/tv/1454763/season/": [],
        "show_all=1": {
            "results": [
                _episode("a" * 32, 0, 1),
                _episode("b" * 32, 0, 2),
            ],
            "has_next": False,
        },
    }
    monkeypatch.setattr(series.httpx, "Client", lambda **kwargs: _FakeClient(pages))  # type: ignore[attr-defined]
    spec = series.resolve_collection(
        "https://rutube.ru/video/" + "e" * 32 + "/?pl_id=1454763",
        "series",
        settings,
    )
    assert spec.series_id == "rutube:1454763"
    assert spec.title == "Playlist series"
    assert [episode.episode for episode in spec.episodes] == [1, 2]
    assert all(episode.season is None for episode in spec.episodes)


def _search_item(title: str, video_id: str, author_id: int = 7) -> dict[str, Any]:
    return {
        "title": title,
        "video_url": f"https://rutube.ru/video/{video_id}/",
        "duration": 1200,
        "author": {"id": author_id},
    }


def test_rutube_search_fallback_builds_full_seasons(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        "/api/video/": {
            "id": "4" * 32,
            "title": "Show 2 сезон 1 серия «Pilot»",
            "duration": 1200,
            "author": {"id": 7},
            "tv_show_id": None,
            "season": None,
            "episode": None,
        },
    }
    search = {
        "Show": [
            _search_item("Show 1 сезон 1 серия «One»", "1" * 32),
            _search_item("Show 2 сезон 1 серия «Pilot»", "4" * 32),
        ],
        "Show 1 сезон": [
            _search_item("Show 1 сезон 1 серия «One»", "1" * 32),
            _search_item("Show 1 сезон 2 серия «Two»", "2" * 32),
        ],
        "Show 2 сезон": [
            _search_item("Show 2 сезон 1 серия «Pilot»", "4" * 32),
            _search_item("Show 2 сезон 2 серия «Next»", "5" * 32),
        ],
    }
    monkeypatch.setattr(
        series.httpx,  # type: ignore[attr-defined]
        "Client",
        lambda **kwargs: _FakeClient(pages, search),
    )
    url = "https://rutube.ru/video/" + "4" * 32 + "/"

    season_spec = series.resolve_collection(url, "season", settings)
    assert _keys(season_spec) == [(2, 1), (2, 2)]
    assert season_spec.episodes[0].url == url

    full_spec = series.resolve_collection(url, "series", settings)
    assert _keys(full_spec) == [(1, 1), (1, 2), (2, 1), (2, 2)]
    assert full_spec.series_id.startswith("rutube-search:7:")
    assert full_spec.episodes[0].season == 1
    assert full_spec.episodes[-1].episode == 2


def _keys(spec: series.CollectionSpec) -> list[tuple[int | None, int | None]]:
    return [(episode.season, episode.episode) for episode in spec.episodes]


def test_rutube_tv_search_uses_series_entity(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        "/api/video/": {
            "id": "4" * 32,
            "title": "Show 2 сезон 1 серия «Pilot»",
            "duration": 1200,
            "author": {"id": 7},
            "tv_show_id": None,
            "season": None,
            "episode": None,
        },
        "/api/metainfo/tv/465326/video": {
            "results": [
                _search_item("Show 1 сезон 1 серия «One»", "1" * 32),
                _search_item("Show 1 сезон 2 серия «Two»", "2" * 32),
                _search_item("Show 2 сезон 1 серия «Pilot»", "4" * 32),
                _search_item("Show 2 сезон 2 серия «Next»", "5" * 32),
                _search_item("Show: OVA 1 серия «Extra»", "6" * 32),
            ],
            "has_next": False,
        },
    }
    tv_search = [
        {
            "id": 465326,
            "name": "Show (аниме-сериал, 2012)",
            "type": {"name": "playlist_series"},
        }
    ]
    monkeypatch.setattr(
        series.httpx,  # type: ignore[attr-defined]
        "Client",
        lambda **kwargs: _FakeClient(pages, tv_search=tv_search),
    )
    url = "https://rutube.ru/video/" + "4" * 32 + "/"

    season_spec = series.resolve_collection(url, "season", settings)
    assert season_spec.series_id == "rutube:465326"
    assert _keys(season_spec) == [(2, 1), (2, 2)]

    full_spec = series.resolve_collection(url, "series", settings)
    assert _keys(full_spec) == [(1, 1), (1, 2), (2, 1), (2, 2)]
    assert full_spec.title == "Show (аниме-сериал, 2012)"


class _VkResponse:
    status_code = 200

    def __init__(self, payload: dict[str, Any], section: str) -> None:
        self.content = json.dumps({"payload": [0, [payload]]}, ensure_ascii=False).encode(
            "cp1251",
            errors="replace",
        )
        self.section = section


class _FakeVkClient:
    def __init__(
        self,
        items: list[list[Any]],
        section: str,
        show_title: str = "",
    ) -> None:
        self.items = items
        self.section = section
        self.show_title = show_title

    def __enter__(self) -> _FakeVkClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(
        self,
        url: str,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _VkResponse:
        del url, headers
        if (data or {}).get("act") == "show":
            content = json.dumps(
                {"payload": [0, [self.show_title, "", ""]]},
                ensure_ascii=False,
            ).encode("cp1251", errors="replace")
            response = _VkResponse.__new__(_VkResponse)
            response.content = content
            response.status_code = 200
            return response
        offset = int((data or {}).get("offset") or 0)
        batch = self.items[offset:]
        block = {
            "count": len(batch),
            "total": len(self.items),
            "list": batch,
        }
        return _VkResponse({self.section: block}, self.section)


def test_vk_single_video_without_episode_is_rejected(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        series.httpx,  # type: ignore[attr-defined]
        "Client",
        lambda **kwargs: _FakeVkClient(
            [],
            "all",
            show_title="Show 1 сезон (марафон)",
        ),
    )
    with pytest.raises(UnsupportedMediaError, match="single video"):
        series.resolve_collection(
            "https://vkvideo.ru/video-1_2",
            "series",
            settings,
        )


def test_vk_playlist_orders_episodes_and_skips_mixed_items(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    section = "playlist_358"
    items = [
        [1, 103, "", "Show 1 сезон 3 серия", 0, "24:00"],
        [1, 101, "", "Show 1 сезон 1 серия", 0, "24:00"],
        [1, 102, "", "Show 1 сезон 2 серия", 0, "24:00"],
        [1, 200, "", "Show 1 сезон (марафон)", 0, "4:00:00"],
        [1, 101, "", "Show 1 сезон 1 серия", 0, "24:00"],
    ]
    monkeypatch.setattr(
        series.httpx,  # type: ignore[attr-defined]
        "Client",
        lambda **kwargs: _FakeVkClient(items, section),
    )

    spec = series.resolve_collection(
        "https://vkvideo.ru/playlist/1_358",
        "series",
        settings,
    )
    assert spec.series_id == "vk:1:358"
    assert spec.title == "Show"
    assert _keys(spec) == [(1, 1), (1, 2), (1, 3)]


def test_vk_playlist_season_url_uses_season_section(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    section = "playlist_111_season_1"
    items = [
        [1, 101, "", "Show. Сезон 1. Серия 1", 0, "24:00"],
        [1, 102, "", "Show. Сезон 1. Серия 2", 0, "24:00"],
    ]
    monkeypatch.setattr(
        series.httpx,  # type: ignore[attr-defined]
        "Client",
        lambda **kwargs: _FakeVkClient(items, section),
    )
    spec = series.resolve_collection(
        "https://vkvideo.ru/playlist/1_111/season_1",
        "season",
        settings,
    )
    assert spec.series_id == "vk:1:111"
    assert spec.season == 1
    assert _keys(spec) == [(1, 1), (1, 2)]


def test_vk_community_fallback_collects_episodes(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [
        [1, 102, "", "Show. Сезон 1. Серия 2", 0, "24:00"],
        [1, 101, "", "Show. Сезон 1. Серия 1", 0, "24:00"],
        [1, 103, "", "Show. Сезон 1. Серия 3", 0, "24:00"],
    ]
    monkeypatch.setattr(
        series.httpx,  # type: ignore[attr-defined]
        "Client",
        lambda **kwargs: _FakeVkClient(
            items,
            "all",
            show_title="Show. Сезон 1. Серия 2",
        ),
    )
    spec = series.resolve_collection(
        "https://vkvideo.ru/video-1_102",
        "season",
        settings,
    )
    assert spec.series_id.startswith("vk-community:-1:")
    assert spec.season == 1
    assert _keys(spec) == [(1, 1), (1, 2), (1, 3)]
    assert spec.episodes[1].url == "https://vkvideo.ru/video-1_102"
