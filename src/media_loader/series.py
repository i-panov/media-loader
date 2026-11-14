from __future__ import annotations

import html
import json
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from typing import Any, cast
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from yt_dlp import YoutubeDL

from media_loader.canonical import canonicalize_url
from media_loader.config import Settings
from media_loader.errors import DownloadFailedError, UnsupportedMediaError

_RUTUBE_HOSTS = {"rutube.ru", "www.rutube.ru"}
_RUTUBE_VIDEO_RE = re.compile(
    r"^https?://(?:www\.)?rutube\.ru/(?:(?:live/)?video|(?:play/)?embed)/(?P<id>[0-9a-z]{32})"
)
_RUTUBE_TV_RE = re.compile(r"^https?://(?:www\.)?rutube\.ru/metainfo/tv/(?P<id>\d+)")
_RUTUBE_PLAYLIST_RE = re.compile(r"^https?://(?:www\.)?rutube\.ru/plst/(?P<id>\d+)")
_VK_HOSTS = {"vk.com", "www.vk.com", "m.vk.com", "vkvideo.ru", "www.vkvideo.ru"}
_VK_PLAYLIST_RE = re.compile(
    r"^https?://(?:www\.)?(?:vk\.com/video/playlist|vkvideo\.ru/playlist)/"
    r"(?P<owner>-?\d+)_(?P<id>-?\d+)(?:/season_(?P<season>\d+))?"
)
_VK_VIDEO_RE = re.compile(
    r"^https?://(?:www\.)?(?:vk\.com/video|vkvideo\.ru/video)(?P<owner>-?\d+)_(?P<video>\d+)"
)
_VK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://vk.com/al_video.php",
}
_VK_SEASON_FIRST_RE = re.compile(
    r"^(?P<base>.+?)[\s.,:;-]+сезон[\s.,:;-]*(?P<season>\d+)"
    r"[\s.,:;-]+серия[\s.,:;-]*(?P<episode>\d+)",
    re.IGNORECASE,
)
_VK_SEASON_SECOND_RE = re.compile(
    r"^(?P<base>.+?)[\s.,:;-]+(?P<season>\d+)[\s.,:;-]*сезон"
    r"[\s.,:;-]+(?P<episode>\d+)[\s.,:;-]*серия",
    re.IGNORECASE,
)
_RUTUBE_API = "https://rutube.ru/api"
_RUTUBE_HEADERS = {"User-Agent": "media-loader/0.1 (+https://github.com/i-panov/media-loader)"}
_EPISODE_TITLE_RE = re.compile(
    r"^(?P<base>.+?)[\s,]+(?P<season>\d+)\s+сезон[\s,]+(?P<episode>\d+)\s+серия\b",
    re.IGNORECASE,
)
_SCOPE_SEASON = "season"


@dataclass(frozen=True)
class EpisodeRef:
    url: str
    title: str
    season: int | None
    episode: int | None
    duration: float | None
    position: int = 0


@dataclass(frozen=True)
class CollectionSpec:
    series_id: str
    title: str
    season: int | None
    episodes: tuple[EpisodeRef, ...]


def spec_to_dict(spec: CollectionSpec) -> dict[str, Any]:
    return {
        "series_id": spec.series_id,
        "title": spec.title,
        "season": spec.season,
        "episodes": [asdict(episode) for episode in spec.episodes],
    }


def spec_from_dict(data: dict[str, Any]) -> CollectionSpec:
    episodes = tuple(
        EpisodeRef(
            url=str(item["url"]),
            title=str(item.get("title") or ""),
            season=_int_or_none(item.get("season")),
            episode=_int_or_none(item.get("episode")),
            duration=_float_or_none(item.get("duration")),
            position=int(item.get("position") or 0),
        )
        for item in data.get("episodes") or []
        if isinstance(item, dict) and item.get("url")
    )
    return CollectionSpec(
        series_id=str(data.get("series_id") or ""),
        title=str(data.get("title") or ""),
        season=_int_or_none(data.get("season")),
        episodes=episodes,
    )


def resolve_collection(url: str, scope: str, settings: Settings) -> CollectionSpec:
    canonical_url = canonicalize_url(url)
    host = (urlsplit(canonical_url).hostname or "").lower()
    if host in _VK_HOSTS:
        playlist_match = _VK_PLAYLIST_RE.match(canonical_url)
        if playlist_match:
            return _resolve_vk_playlist(
                playlist_match.group("owner"),
                playlist_match.group("id"),
                _int_or_none(playlist_match.group("season")),
                scope,
                settings,
            )
        video_match = _VK_VIDEO_RE.match(canonical_url)
        if video_match:
            return _resolve_vk_video(
                video_match.group("owner"),
                video_match.group("video"),
                scope,
                settings,
            )
    if host in _RUTUBE_HOSTS:
        if _RUTUBE_VIDEO_RE.match(canonical_url):
            return _resolve_rutube_video(canonical_url, scope, settings)
        tv_match = _RUTUBE_TV_RE.match(canonical_url)
        if tv_match:
            return _resolve_rutube_tv(tv_match.group("id"), scope, settings, None)
        playlist_match = _RUTUBE_PLAYLIST_RE.match(canonical_url)
        if playlist_match:
            try:
                return _resolve_rutube_tv(
                    playlist_match.group("id"),
                    scope,
                    settings,
                    None,
                )
            except DownloadFailedError:
                pass
    return _resolve_generic(canonical_url, scope, settings)


def _resolve_rutube_video(url: str, scope: str, settings: Settings) -> CollectionSpec:
    match = _RUTUBE_VIDEO_RE.match(url)
    if match is None:
        raise UnsupportedMediaError("unsupported RuTube URL")
    video_id = match.group("id")
    with httpx.Client(
        timeout=settings.series_timeout,
        follow_redirects=True,
        headers=_RUTUBE_HEADERS,
    ) as client:
        video = _get_json(client, f"{_RUTUBE_API}/video/{video_id}/?format=json")
        tv_id = _int_or_none(video.get("tv_show_id"))
        season_hint = _int_or_none(video.get("season"))
        if tv_id is None:
            playlist_id = (parse_qs(urlsplit(url).query).get("pl_id") or [""])[0]
            if playlist_id.isdigit():
                try:
                    return _resolve_rutube_tv(
                        playlist_id,
                        scope,
                        settings,
                        season_hint,
                    )
                except DownloadFailedError:
                    pass
            try:
                series = _resolve_rutube_tv_search(
                    client,
                    video,
                    video_id,
                    scope,
                )
            except DownloadFailedError:
                series = None
            if series is not None:
                return series
            fallback = _resolve_rutube_search(
                client,
                video,
                video_id,
                scope,
            )
            if fallback is not None:
                return fallback
            raise UnsupportedMediaError(
                "this RuTube video is not linked to a series; "
                "open the series page and retry from there"
            )
    return _resolve_rutube_tv(str(tv_id), scope, settings, season_hint)


def _resolve_rutube_tv(
    tv_id: str,
    scope: str,
    settings: Settings,
    season_hint: int | None,
) -> CollectionSpec:
    with httpx.Client(
        timeout=settings.series_timeout,
        follow_redirects=True,
        headers=_RUTUBE_HEADERS,
    ) as client:
        meta = _get_json(client, f"{_RUTUBE_API}/metainfo/tv/{tv_id}/?format=json")
        title = str(meta.get("name") or f"RuTube {tv_id}")
        season_numbers = _rutube_season_numbers(client, tv_id)
        if scope == _SCOPE_SEASON:
            if season_hint is None:
                if len(season_numbers) == 1:
                    season_hint = season_numbers[0]
                elif not season_numbers:
                    season_hint = None
                else:
                    raise UnsupportedMediaError(
                        "cannot determine the season for this link; "
                        "pass a link to a specific episode"
                    )
            episodes = _rutube_episodes_for_season(client, tv_id, season_hint)
        else:
            episodes = _rutube_all_episodes(client, tv_id, season_numbers)
        selected_season = season_hint if scope == _SCOPE_SEASON else None
    final_episodes = _finalize_episodes(episodes)
    if not final_episodes:
        raise UnsupportedMediaError("no episodes found for this RuTube series")
    return CollectionSpec(
        series_id=f"rutube:{tv_id}",
        title=title,
        season=selected_season,
        episodes=final_episodes,
    )


def _rutube_season_numbers(client: httpx.Client, tv_id: str) -> tuple[int, ...]:
    payload = _get_json(client, f"{_RUTUBE_API}/metainfo/tv/{tv_id}/season/?format=json")
    if not isinstance(payload, list):
        return ()
    numbers = {
        number
        for item in payload
        if isinstance(item, dict)
        for number in (_int_or_none(item.get("number")),)
        if number is not None
    }
    return tuple(sorted(numbers))


def _rutube_all_episodes(
    client: httpx.Client,
    tv_id: str,
    season_numbers: tuple[int, ...],
) -> list[EpisodeRef]:
    if season_numbers:
        episodes: list[EpisodeRef] = []
        for number in season_numbers:
            episodes.extend(_rutube_episodes_for_season(client, tv_id, number))
        return episodes
    return _rutube_episodes_for_season(client, tv_id, None)


def _rutube_episodes_for_season(
    client: httpx.Client,
    tv_id: str,
    season: int | None,
) -> list[EpisodeRef]:
    if season is None:
        url = f"{_RUTUBE_API}/metainfo/tv/{tv_id}/video?show_all=1&format=json"
    else:
        url = f"{_RUTUBE_API}/metainfo/tv/{tv_id}/video?season={season}&show_all=1&format=json"
    episodes: list[EpisodeRef] = []
    while url:
        payload = _get_json(client, url)
        if not isinstance(payload, dict):
            break
        for item in payload.get("results") or []:
            episode = _rutube_episode(item, season)
            if episode is not None:
                episodes.append(episode)
        next_url = str(payload.get("next") or "")
        url = next_url if next_url.startswith("https://") and payload.get("has_next") else ""
    return episodes


def _rutube_episode(item: Any, fallback_season: int | None) -> EpisodeRef | None:
    if not isinstance(item, dict):
        return None
    url = item.get("video_url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    season = _int_or_none(item.get("season"))
    if season is None:
        season = fallback_season
    return EpisodeRef(
        url=url,
        title=str(item.get("title") or ""),
        season=season or None,
        episode=_int_or_none(item.get("episode")) or None,
        duration=_float_or_none(item.get("duration")),
    )


def _pick_tv_series(results: list[Any], base: str) -> dict[str, Any] | None:
    normalized_base = _normalize_title(base)
    candidates: list[tuple[int, dict[str, Any]]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        name = _normalize_title(str(item.get("name") or ""))
        if name != normalized_base and not name.startswith(f"{normalized_base} "):
            continue
        series_type = str(((item.get("type") or {}).get("name")) or "")
        score = 2 if series_type in {"series", "playlist_series"} else 1
        candidates.append((score, item))
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0])[1]


def _rutube_tv_episodes_by_title(
    client: httpx.Client,
    tv_id: str,
    normalized_base: str,
) -> list[EpisodeRef]:
    result: list[EpisodeRef] = []
    url = f"{_RUTUBE_API}/metainfo/tv/{tv_id}/video?format=json"
    for _ in range(20):
        payload = _get_json(client, url)
        if not isinstance(payload, dict):
            break
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            parsed = _parse_episode_title(str(item.get("title") or ""))
            if parsed is None:
                continue
            base, season, episode = parsed
            if _normalize_title(base) != normalized_base:
                continue
            video_url = item.get("video_url")
            if not isinstance(video_url, str) or not video_url.startswith(("http://", "https://")):
                continue
            result.append(
                EpisodeRef(
                    url=video_url,
                    title=str(item.get("title") or ""),
                    season=season,
                    episode=episode,
                    duration=_float_or_none(item.get("duration")),
                    position=len(result),
                )
            )
        next_url = str(payload.get("next") or "")
        if not payload.get("has_next") or not next_url.startswith("https://"):
            break
        url = next_url
    return result


def _resolve_rutube_tv_search(
    client: httpx.Client,
    video: dict[str, Any],
    video_id: str,
    scope: str,
) -> CollectionSpec | None:
    title = str(video.get("title") or "")
    parsed = _parse_episode_title(title)
    if parsed is None:
        return None
    base, source_season, source_episode = parsed
    payload = _get_json(
        client,
        f"{_RUTUBE_API}/metainfo/tv/",
        {"search": base, "format": "json"},
    )
    if not isinstance(payload, dict):
        return None
    found = _pick_tv_series(list(payload.get("results") or []), base)
    if found is None:
        return None
    tv_id = _int_or_none(found.get("id"))
    if tv_id is None:
        return None
    normalized_base = _normalize_title(base)
    episodes = _rutube_tv_episodes_by_title(client, str(tv_id), normalized_base)
    if not episodes:
        return None

    source_url = f"https://rutube.ru/video/{video_id}/"
    by_episode: dict[tuple[int, int], EpisodeRef] = {
        (source_season, source_episode): EpisodeRef(
            url=source_url,
            title=title,
            season=source_season,
            episode=source_episode,
            duration=_float_or_none(video.get("duration")),
            position=0,
        )
    }
    for episode in episodes:
        if episode.season is None or episode.episode is None:
            continue
        key = (episode.season, episode.episode)
        current = by_episode.get(key)
        if current is None:
            by_episode[key] = episode
            continue
        if current.url == source_url:
            continue
        if (episode.duration or 0) > (current.duration or 0):
            by_episode[key] = episode

    selected = list(by_episode.values())
    if scope == "season":
        selected = [episode for episode in selected if episode.season == source_season]
    if not selected:
        return None
    return CollectionSpec(
        series_id=f"rutube:{tv_id}",
        title=str(found.get("name") or base),
        season=source_season if scope == "season" else None,
        episodes=_finalize_episodes(selected),
    )


def _parse_episode_title(title: str) -> tuple[str, int, int] | None:
    match = _EPISODE_TITLE_RE.match(title.strip())
    if match is None:
        return None
    base = match.group("base").strip(" .,:;—-")
    if not base:
        return None
    return base, int(match.group("season")), int(match.group("episode"))


def _normalize_title(value: str) -> str:
    value = value.lower().replace("\u0451", "\u0435")
    value = re.sub(r"\s+", " ", value)
    return value.strip(" .,:;—-")


def _resolve_rutube_search(
    client: httpx.Client,
    video: dict[str, Any],
    video_id: str,
    scope: str,
) -> CollectionSpec | None:
    title = str(video.get("title") or "")
    parsed = _parse_episode_title(title)
    if parsed is None:
        return None
    base, source_season, source_episode = parsed
    author_id = _int_or_none((video.get("author") or {}).get("id"))
    if author_id is None:
        return None
    normalized_base = _normalize_title(base)
    if scope == "season":
        episodes = _rutube_search_episodes(
            client,
            f"{base} {source_season} сезон",
            author_id,
            normalized_base,
            season_filter=source_season,
        )
    else:
        episodes = _rutube_search_episodes(client, base, author_id, normalized_base)
        seasons = {source_season} | {
            episode.season for episode in episodes if episode.season is not None
        }
        if seasons:
            for season in range(1, max(seasons) + 2):
                episodes.extend(
                    _rutube_search_episodes(
                        client,
                        f"{base} {season} сезон",
                        author_id,
                        normalized_base,
                        season_filter=season,
                    )
                )
    if not episodes:
        return None

    source_url = f"https://rutube.ru/video/{video_id}/"
    source = EpisodeRef(
        url=source_url,
        title=title,
        season=source_season,
        episode=source_episode,
        duration=_float_or_none(video.get("duration")),
        position=0,
    )
    by_episode: dict[tuple[int, int], EpisodeRef] = {(source_season, source_episode): source}
    for episode in episodes:
        if episode.season is None or episode.episode is None:
            continue
        key = (episode.season, episode.episode)
        current = by_episode.get(key)
        if current is None:
            by_episode[key] = episode
            continue
        if current.url == source_url:
            continue
        if (episode.duration or 0) > (current.duration or 0):
            by_episode[key] = episode

    selected = list(by_episode.values())
    if scope == "season":
        selected = [episode for episode in selected if episode.season == source_season]
    if not selected:
        return None
    return CollectionSpec(
        series_id=f"rutube-search:{author_id}:{normalized_base}",
        title=base,
        season=source_season if scope == "season" else None,
        episodes=_finalize_episodes(selected),
    )


def _rutube_search_episodes(
    client: httpx.Client,
    query: str,
    author_id: int,
    normalized_base: str,
    season_filter: int | None = None,
) -> list[EpisodeRef]:
    result: list[EpisodeRef] = []
    url = f"{_RUTUBE_API}/search/video/"
    params: dict[str, Any] | None = {"query": query, "format": "json", "page": 1}
    for _ in range(10):
        payload = _get_json(client, url, params)
        if not isinstance(payload, dict):
            break
        for item in payload.get("results") or []:
            if not isinstance(item, dict) or item.get("is_livestream") or item.get("is_deleted"):
                continue
            author = item.get("author") or {}
            if _int_or_none(author.get("id")) != author_id:
                continue
            parsed = _parse_episode_title(str(item.get("title") or ""))
            if parsed is None:
                continue
            base, season, episode = parsed
            if _normalize_title(base) != normalized_base:
                continue
            if season_filter is not None and season != season_filter:
                continue
            video_url = item.get("video_url")
            if not isinstance(video_url, str) or not video_url.startswith(("http://", "https://")):
                continue
            result.append(
                EpisodeRef(
                    url=video_url,
                    title=str(item.get("title") or ""),
                    season=season,
                    episode=episode,
                    duration=_float_or_none(item.get("duration")),
                    position=len(result),
                )
            )
        next_url = str(payload.get("next") or "")
        if not payload.get("has_next") or not next_url.startswith("https://"):
            break
        url = next_url
        params = None
    return result


def _vk_payload(client: httpx.Client, data: dict[str, Any]) -> Any:
    try:
        response = client.post(
            "https://vk.com/al_video.php",
            data=data,
            headers=_VK_HEADERS,
        )
    except httpx.HTTPError as exc:
        raise DownloadFailedError(f"VK API request failed: {exc}") from exc
    if response.status_code >= 400:
        raise DownloadFailedError(f"VK API returned HTTP {response.status_code}")
    text = response.content.decode("cp1251", errors="replace")
    try:
        payload = json.loads(text)["payload"]
    except (KeyError, ValueError) as exc:
        raise DownloadFailedError("VK API returned invalid JSON") from exc
    if not isinstance(payload, list) or len(payload) < 2:
        raise DownloadFailedError("VK API returned an unexpected payload")
    if str(payload[0]) == "3":
        raise UnsupportedMediaError("VK requires authentication for this request")
    if str(payload[0]) != "0":
        raise DownloadFailedError("VK API returned an error")
    return payload[1]


def _vk_block(payload: Any, section: str) -> dict[str, Any]:
    if not isinstance(payload, list) or not payload:
        return {}
    container = payload[0]
    if not isinstance(container, dict):
        return {}
    block = container.get(section)
    return block if isinstance(block, dict) else {}


def _vk_duration(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    if len(numbers) == 2:
        return float(numbers[0] * 60 + numbers[1])
    if len(numbers) == 3:
        return float(numbers[0] * 3600 + numbers[1] * 60 + numbers[2])
    return None


def _parse_vk_episode_title(title: str) -> tuple[str, int, int] | None:
    for pattern in (_VK_SEASON_FIRST_RE, _VK_SEASON_SECOND_RE):
        match = pattern.match(title.strip())
        if match is None:
            continue
        base = match.group("base").strip(" .,:;—-")
        if not base:
            return None
        return base, int(match.group("season")), int(match.group("episode"))
    return None


def _vk_episode(item: Any, position: int) -> EpisodeRef | None:
    if not isinstance(item, list) or len(item) < 6:
        return None
    owner = _int_or_none(item[0])
    video_id = _int_or_none(item[1])
    title = html.unescape(str(item[3] or "")).strip()
    if owner is None or video_id is None or not title:
        return None
    parsed = _parse_vk_episode_title(title)
    return EpisodeRef(
        url=f"https://vkvideo.ru/video{owner}_{video_id}",
        title=title,
        season=parsed[1] if parsed else None,
        episode=parsed[2] if parsed else None,
        duration=_vk_duration(item[5]),
        position=position,
    )


def _resolve_vk_playlist(
    owner: str,
    playlist_id: str,
    season: int | None,
    scope: str,
    settings: Settings,
) -> CollectionSpec:
    section = f"playlist_{playlist_id}"
    if season is not None and scope == "season":
        section = f"{section}_season_{season}"
    items: list[EpisodeRef] = []
    with httpx.Client(timeout=settings.series_timeout) as client:
        offset = 0
        for _ in range(50):
            payload = _vk_payload(
                client,
                {
                    "act": "load_videos_silent",
                    "oid": owner,
                    "section": section,
                    "offset": offset,
                    "al": 1,
                },
            )
            block = _vk_block(payload, section)
            batch = block.get("list") or []
            for item in batch:
                episode = _vk_episode(item, len(items))
                if episode is not None:
                    items.append(episode)
            count = _int_or_none(block.get("count")) or 0
            total = _int_or_none(block.get("total")) or 0
            if not batch or count <= 0:
                break
            offset += count
            if total and offset >= total:
                break
    if not items:
        raise UnsupportedMediaError("VK playlist has no available episodes")
    parsed_items = [
        episode for episode in items if episode.season is not None and episode.episode is not None
    ]
    if parsed_items:
        items = parsed_items

    bases = [_parse_vk_episode_title(episode.title) for episode in items]
    base = next((parsed[0] for parsed in bases if parsed), "")
    title = base or f"VK playlist {owner}_{playlist_id}"
    seasons = {episode.season for episode in items if episode.season is not None}
    selected_season: int | None = None
    if scope == "season":
        if season is not None:
            selected_season = season
        elif len(seasons) > 1:
            raise UnsupportedMediaError(
                "VK playlist contains several seasons; pass a /season_N link"
            )
        else:
            selected_season = next(iter(seasons), None)
        if selected_season is not None:
            items = [episode for episode in items if episode.season == selected_season]
    if not items:
        raise UnsupportedMediaError("VK playlist has no episodes for the requested season")
    return CollectionSpec(
        series_id=f"vk:{owner}:{playlist_id}",
        title=title,
        season=selected_season,
        episodes=_finalize_episodes(items),
    )


def _vk_video_title(client: httpx.Client, owner: str, video_id: str) -> str:
    payload = _vk_payload(
        client,
        {"act": "show", "video": f"{owner}_{video_id}", "al": 1},
    )
    if isinstance(payload, list) and payload and isinstance(payload[0], str):
        return payload[0]
    return ""


def _vk_community_episodes(
    client: httpx.Client,
    owner: str,
    base: str,
) -> list[EpisodeRef]:
    normalized_base = _normalize_title(base)
    result: list[EpisodeRef] = []
    offset = 0
    for _ in range(30):
        payload = _vk_payload(
            client,
            {
                "act": "load_videos_silent",
                "oid": owner,
                "section": "all",
                "offset": offset,
                "al": 1,
            },
        )
        block = _vk_block(payload, "all")
        batch = block.get("list") or []
        for item in batch:
            episode = _vk_episode(item, len(result))
            if episode is None:
                continue
            parsed = _parse_vk_episode_title(episode.title)
            if parsed is None or _normalize_title(parsed[0]) != normalized_base:
                continue
            result.append(episode)
        count = _int_or_none(block.get("count")) or 0
        total = _int_or_none(block.get("total")) or 0
        if not batch or count <= 0:
            break
        offset += count
        if total and offset >= total:
            break
    return result


def _resolve_vk_video(
    owner: str,
    video_id: str,
    scope: str,
    settings: Settings,
) -> CollectionSpec:
    with httpx.Client(timeout=settings.series_timeout) as client:
        title = _vk_video_title(client, owner, video_id)
        parsed = _parse_vk_episode_title(title)
        if parsed is None:
            raise UnsupportedMediaError(
                "VK Video does not expose a season/episode structure for a single video; "
                "open a playlist with episodes and pass its link"
            )
        base, source_season, source_episode = parsed
        episodes = _vk_community_episodes(client, owner, base)
    if not episodes:
        raise UnsupportedMediaError(
            "VK did not return separate episodes for this video; "
            "open a playlist with episodes and pass its link"
        )
    source_url = f"https://vkvideo.ru/video{owner}_{video_id}"
    parsed_items = [
        episode
        for episode in episodes
        if episode.season is not None and episode.episode is not None
    ]
    by_episode: dict[tuple[int, int], EpisodeRef] = {}
    for episode in parsed_items:
        key = (episode.season or 0, episode.episode or 0)
        current = by_episode.get(key)
        if current is None or (
            episode.url != source_url and (episode.duration or 0) > (current.duration or 0)
        ):
            by_episode[key] = episode
    source = EpisodeRef(
        url=source_url,
        title=title,
        season=source_season,
        episode=source_episode,
        duration=None,
        position=0,
    )
    by_episode[(source_season, source_episode)] = source
    selected = list(by_episode.values())
    if scope == "season":
        selected = [episode for episode in selected if episode.season == source_season]
    if not selected:
        raise UnsupportedMediaError("VK has no episodes for the requested season")
    return CollectionSpec(
        series_id=f"vk-community:{owner}:{_normalize_title(base)}",
        title=base,
        season=source_season if scope == "season" else None,
        episodes=_finalize_episodes(selected),
    )


def _resolve_generic(url: str, scope: str, settings: Settings) -> CollectionSpec:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": False,
        "ignoreerrors": False,
        "socket_timeout": settings.series_timeout,
    }
    if settings.ytdlp_cookies_file.is_file():
        options["cookiefile"] = str(settings.ytdlp_cookies_file)
    try:
        with YoutubeDL(cast(Any, options)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise DownloadFailedError(f"failed to read playlist: {exc}") from exc
    if not info:
        raise UnsupportedMediaError("failed to read playlist")

    is_playlist = info.get("_type") == "playlist" or bool(info.get("entries"))
    if is_playlist:
        series_id = str(info.get("id") or info.get("playlist_id") or url)
        title = str(info.get("title") or info.get("playlist_title") or "playlist")
        season_hint: int | None = None
    else:
        series_id = str(info.get("playlist_id") or info.get("series_id") or "")
        title = str(info.get("playlist_title") or info.get("series") or info.get("title") or "")
        season_hint = _int_or_none(info.get("season_number") or info.get("season"))
        if not series_id:
            raise UnsupportedMediaError(
                "cannot determine a season or series for this URL; "
                "pass a playlist, a season, or a supported episode link"
            )
        if scope != "episode":
            raise UnsupportedMediaError(
                "cannot enumerate episodes from a single video URL; "
                "pass a playlist or a season link"
            )

    entries = list(_iter_entries(cast(dict[str, Any], info)))
    if not entries:
        raise UnsupportedMediaError("playlist has no episodes")
    episodes = _generic_episodes(entries, url)
    if not episodes:
        raise UnsupportedMediaError("playlist has no downloadable episodes")

    selected_season: int | None = None
    if scope == _SCOPE_SEASON:
        if season_hint is not None:
            selected_season = season_hint
        else:
            distinct = {episode.season for episode in episodes if episode.season is not None}
            if len(distinct) == 1:
                selected_season = next(iter(distinct))
            elif distinct:
                raise UnsupportedMediaError(
                    "cannot determine the season for this link; pass a link to a specific episode"
                )
        if selected_season is not None:
            episodes = [ep for ep in episodes if ep.season == selected_season]

    final_episodes = _finalize_episodes(episodes)
    if not final_episodes:
        raise UnsupportedMediaError("no episodes found for the requested season")
    return CollectionSpec(
        series_id=f"generic:{series_id}",
        title=title,
        season=selected_season,
        episodes=final_episodes,
    )


def _iter_entries(info: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for entry in info.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("entries"):
            yield from _iter_entries(entry)
            continue
        yield entry


def _generic_episodes(entries: list[dict[str, Any]], base_url: str) -> list[EpisodeRef]:
    result: list[EpisodeRef] = []
    for position, entry in enumerate(entries):
        raw_url = entry.get("webpage_url") or entry.get("url")
        if not isinstance(raw_url, str) or not raw_url:
            continue
        episode_url = urljoin(base_url, raw_url)
        if not episode_url.startswith(("http://", "https://")):
            continue
        season = _int_or_none(entry.get("season_number") or entry.get("season"))
        episode = _int_or_none(entry.get("episode_number") or entry.get("episode"))
        result.append(
            EpisodeRef(
                url=episode_url,
                title=str(entry.get("title") or f"Episode {position + 1}"),
                season=season,
                episode=episode,
                duration=_float_or_none(entry.get("duration")),
                position=position,
            )
        )
    return result


def _finalize_episodes(episodes: list[EpisodeRef]) -> tuple[EpisodeRef, ...]:
    counters: dict[int | None, int] = {}
    numbered: list[EpisodeRef] = []
    for episode in episodes:
        number = episode.episode
        if number is None:
            counters[episode.season] = counters.get(episode.season, 0) + 1
            number = counters[episode.season]
        numbered.append(
            EpisodeRef(
                url=episode.url,
                title=episode.title,
                season=episode.season,
                episode=number,
                duration=episode.duration,
                position=episode.position,
            )
        )
    unique: dict[str, EpisodeRef] = {}
    for episode in numbered:
        unique.setdefault(episode.url, episode)
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            item.season if item.season is not None else 0,
            item.episode if item.episode is not None else 0,
            item.position,
        ),
    )
    return tuple(ordered)


def _get_json(
    client: httpx.Client,
    url: str,
    params: dict[str, Any] | None = None,
) -> Any:
    try:
        response = client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise DownloadFailedError(f"series API request failed: {exc}") from exc
    if response.status_code >= 400:
        raise DownloadFailedError(f"series API returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise DownloadFailedError("series API returned invalid JSON") from exc


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None
