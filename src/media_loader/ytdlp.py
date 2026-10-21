from __future__ import annotations

import logging
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from media_loader.config import Settings
from media_loader.errors import LeaseLostError, UnsupportedMediaError
from media_loader.progress import DownloadProgressTracker, ProgressReporter

LOGGER = logging.getLogger(__name__)

_VIDEO_EXTENSIONS = {"mp4", "mkv", "webm", "mov", "avi", "flv", "m4v", "ts"}

_IGNORED_SUFFIXES = {
    ".part",
    ".ytdl",
    ".temp",
    ".tmp",
    ".json",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".vtt",
    ".srt",
    ".ass",
    ".lrc",
}
_NON_RETRYABLE_MARKERS = (
    "unsupported url",
    "is not a valid url",
    "private video",
    "video unavailable",
    "this video is unavailable",
    "not available",
    "http error 404",
    "not found",
    "sign in",
    "login required",
    "age-restricted",
    "age restricted",
    "drm",
    "copyright",
    "playlist",
    "requested format is not available",
    "no video formats found",
)


@dataclass(frozen=True)
class MediaMetadata:
    title: str
    description: str | None
    duration: float | None
    thumbnail: str | None
    has_video: bool
    has_audio: bool
    width: int | None
    height: int | None
    max_quality: str | None
    available_qualities: tuple[str, ...]
    video_codec: str | None
    audio_codec: str | None
    details: dict[str, Any]


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    metadata: MediaMetadata
    info: dict[str, Any]


class _YtDlpLogger:
    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug]"):
            LOGGER.debug("%s", msg)

    def warning(self, msg: str) -> None:
        LOGGER.warning("%s", msg)

    def error(self, msg: str) -> None:
        LOGGER.error("%s", msg)


def is_retryable_error(error: BaseException | str) -> bool:
    message = str(error).lower()
    return not any(marker in message for marker in _NON_RETRYABLE_MARKERS)


def _validate_info(info: dict[str, Any] | None) -> dict[str, Any]:
    if not info:
        raise DownloadError("extractor returned no metadata")
    if info.get("_type") == "playlist" or info.get("entries"):
        raise UnsupportedMediaError("playlists are not supported")
    live_status = str(info.get("live_status") or "")
    if info.get("is_live") or live_status in {"is_live", "is_upcoming"}:
        raise UnsupportedMediaError("live streams are not supported")
    return info


def _has_video_formats(info: dict[str, Any]) -> bool:
    formats = info.get("formats") or []
    if any((item.get("vcodec") or "none") != "none" for item in formats if isinstance(item, dict)):
        return True
    return (info.get("vcodec") or "none") != "none"


_DETAIL_FIELDS = (
    "id",
    "display_id",
    "fulltitle",
    "description",
    "webpage_url",
    "original_url",
    "extractor",
    "extractor_key",
    "uploader",
    "uploader_id",
    "uploader_url",
    "channel",
    "channel_id",
    "channel_url",
    "channel_follower_count",
    "channel_is_verified",
    "creator",
    "creator_id",
    "creator_url",
    "duration",
    "duration_string",
    "upload_date",
    "timestamp",
    "release_date",
    "release_timestamp",
    "thumbnail",
    "view_count",
    "like_count",
    "comment_count",
    "repost_count",
    "age_limit",
    "availability",
    "live_status",
    "is_live",
    "was_live",
    "tags",
    "categories",
    "genres",
    "artist",
    "album",
    "album_artist",
    "track",
    "track_number",
    "disc_number",
    "release_year",
    "series",
    "season",
    "season_number",
    "episode",
    "episode_number",
    "cast",
    "director",
    "producer",
    "writer",
    "composer",
    "game",
    "language",
    "license",
    "location",
    "width",
    "height",
    "fps",
    "aspect_ratio",
    "resolution",
    "format",
    "format_id",
    "format_note",
    "vcodec",
    "acodec",
    "ext",
)


def _quality_label(height: int) -> str:
    return f"{height}p"


def _format_summary(item: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "format_id",
        "format_note",
        "ext",
        "resolution",
        "width",
        "height",
        "fps",
        "vcodec",
        "acodec",
        "abr",
        "tbr",
        "filesize",
        "filesize_approx",
        "protocol",
    )
    return {field: item[field] for field in fields if item.get(field) is not None}


def _describe_details(info: dict[str, Any]) -> dict[str, Any]:
    details = {field: info[field] for field in _DETAIL_FIELDS if info.get(field) is not None}
    thumbnails = info.get("thumbnails") or []
    if thumbnails:
        details["thumbnails"] = [
            {
                key: item[key]
                for key in ("id", "url", "width", "height", "preference")
                if item.get(key) is not None
            }
            for item in thumbnails
            if isinstance(item, dict) and item.get("url")
        ]
    formats = info.get("formats") or []
    if formats:
        details["formats"] = [_format_summary(item) for item in formats if isinstance(item, dict)]
    for field in ("subtitles", "automatic_captions"):
        value = info.get(field)
        if isinstance(value, dict) and value:
            details[field] = sorted(value)
    chapters = info.get("chapters") or []
    if chapters:
        details["chapters"] = [
            {
                key: item[key]
                for key in ("title", "start_time", "end_time")
                if item.get(key) is not None
            }
            for item in chapters
            if isinstance(item, dict)
        ]
    return details


def describe_media(info: dict[str, Any]) -> MediaMetadata:
    formats = [item for item in (info.get("formats") or []) if isinstance(item, dict)]
    video_formats = [
        item for item in formats if (item.get("vcodec") or "none") != "none" and item.get("height")
    ]
    audio_formats = [item for item in formats if (item.get("acodec") or "none") != "none"]
    best_video = max(
        video_formats,
        key=lambda item: (
            int(item.get("height") or 0),
            float(item.get("tbr") or 0),
        ),
        default=None,
    )
    best_audio = max(
        audio_formats,
        key=lambda item: (
            float(item.get("abr") or 0),
            float(item.get("tbr") or 0),
        ),
        default=None,
    )
    heights = sorted({int(item["height"]) for item in video_formats if item.get("height")})
    has_video = bool(heights) or (info.get("vcodec") or "none") != "none"
    ext = str(info.get("ext") or "").lower()
    if not has_video and not formats and ext in _VIDEO_EXTENSIONS:
        has_video = True
    has_audio = bool(audio_formats) or (info.get("acodec") or "none") != "none"
    width = int(best_video["width"]) if best_video and best_video.get("width") else None
    height = int(best_video["height"]) if best_video and best_video.get("height") else None
    if width is None and info.get("width"):
        width = int(info["width"])
    if height is None and info.get("height"):
        height = int(info["height"])
    max_height = max(heights) if heights else height
    max_quality = _quality_label(max_height) if max_height else None
    return MediaMetadata(
        title=str(info.get("title") or "media"),
        description=info.get("description"),
        duration=float(info["duration"]) if info.get("duration") else None,
        thumbnail=info.get("thumbnail"),
        has_video=has_video,
        has_audio=has_audio,
        width=width,
        height=height,
        max_quality=max_quality,
        available_qualities=tuple(_quality_label(item) for item in heights),
        video_codec=(
            str(best_video.get("vcodec"))
            if best_video and best_video.get("vcodec") not in (None, "none")
            else None
        ),
        audio_codec=(
            str(best_audio.get("acodec"))
            if best_audio and best_audio.get("acodec") not in (None, "none")
            else None
        ),
        details=_describe_details(info),
    )


VIDEO_FORMAT_SELECTOR = (
    "bv*[protocol^=http][format_note!*=Premium]+ba[protocol^=http]"
    "/b[protocol^=http][format_note!*=Premium]"
    "/bv*[protocol^=http]+ba[protocol^=http]"
    "/b[protocol^=http]"
    "/bv*+ba/b"
)


def _format_selector(info: dict[str, Any]) -> tuple[str, bool]:
    formats = info.get("formats") or []
    if _has_video_formats(info):
        return VIDEO_FORMAT_SELECTOR, True
    if formats:
        return "bestaudio/best", False
    ext = str(info.get("ext") or "").lower()
    if ext in _VIDEO_EXTENSIONS:
        return "best", False
    return "bestaudio/best", False


def _base_options(
    settings: Settings,
    proxy: str | None,
    logger: _YtDlpLogger,
    fast_fail: bool,
    player_client: str | None = None,
) -> dict[str, Any]:
    retries = 0 if fast_fail else 2
    fragment_retries = 0 if fast_fail else 5
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": settings.direct_socket_timeout,
        "retries": retries,
        "fragment_retries": fragment_retries,
        "extractor_retries": retries,
        "noplaylist": True,
        "ignoreerrors": False,
        "logger": logger,
        "nocheckcertificate": False,
    }
    ffmpeg_path = shutil.which(settings.ffmpeg_path)
    if not ffmpeg_path and Path(settings.ffmpeg_path).is_file():
        ffmpeg_path = settings.ffmpeg_path
    if ffmpeg_path:
        options["ffmpeg_location"] = ffmpeg_path
    if proxy:
        options["proxy"] = proxy
    if player_client:
        options["extractor_args"] = {"youtube": {"player_client": [player_client]}}
    if settings.ytdlp_cookies_file.is_file():
        options["cookiefile"] = str(settings.ytdlp_cookies_file)
    return options


def _find_media_file(temp_dir: Path) -> Path:
    candidates = [
        path
        for path in temp_dir.rglob("*")
        if path.is_file()
        and path.suffix.lower() not in _IGNORED_SUFFIXES
        and not any(part.startswith(".") for part in path.relative_to(temp_dir).parts)
    ]
    if not candidates:
        raise DownloadError("downloaded file not found")
    return max(candidates, key=lambda path: path.stat().st_size)


def metadata_to_dict(metadata: MediaMetadata) -> dict[str, Any]:
    result = dict(metadata.details)
    result.update(
        {
            "title": metadata.title,
            "description": metadata.description,
            "duration": metadata.duration,
            "thumbnail": metadata.thumbnail,
            "has_video": metadata.has_video,
            "has_audio": metadata.has_audio,
            "width": metadata.width,
            "height": metadata.height,
            "max_quality": metadata.max_quality,
            "source_quality": metadata.max_quality,
            "available_qualities": list(metadata.available_qualities),
            "video_codec": metadata.video_codec,
            "audio_codec": metadata.audio_codec,
        }
    )
    return {key: value for key, value in result.items() if value is not None}


def probe_source_metadata(
    url: str,
    proxy: str | None,
    settings: Settings,
    fast_fail: bool = False,
    player_client: str | None = None,
    format_selector: str | None = None,
) -> tuple[MediaMetadata, dict[str, Any]]:
    logger = _YtDlpLogger()
    options = _base_options(settings, proxy, logger, fast_fail, player_client)
    options.update({"skip_download": True})
    if format_selector:
        options["format"] = format_selector
    with YoutubeDL(cast(Any, options)) as ydl:
        info = _validate_info(cast(dict[str, Any], ydl.extract_info(url, download=False)))
    return describe_media(info), info


def download_source(
    url: str,
    temp_dir: Path,
    proxy: str | None,
    settings: Settings,
    reporter: ProgressReporter,
    source_key: str,
    fast_fail: bool = False,
    cancel_event: threading.Event | None = None,
    player_client: str | None = None,
) -> DownloadResult:
    if cancel_event is not None and cancel_event.is_set():
        raise LeaseLostError
    temp_dir.mkdir(parents=True, exist_ok=True)
    metadata, info = probe_source_metadata(
        url,
        proxy,
        settings,
        fast_fail,
        player_client=player_client,
    )
    if cancel_event is not None and cancel_event.is_set():
        raise LeaseLostError
    logger = _YtDlpLogger()
    base = _base_options(settings, proxy, logger, fast_fail, player_client)

    format_selector, merge_to_mkv = _format_selector(info)
    tracker = DownloadProgressTracker(reporter, source_key, cancel_event)
    download_opts = dict(base)
    download_opts.update(
        {
            "format": format_selector,
            "paths": {"home": str(temp_dir), "temp": str(temp_dir / ".tmp")},
            "outtmpl": {"default": "%(title).170B [%(id)s].%(ext)s"},
            "restrictfilenames": True,
            "windowsfilenames": True,
            "overwrites": True,
            "continuedl": False,
            "concurrent_fragment_downloads": 1 if proxy else 4,
            "progress_hooks": [tracker.hook],
        }
    )
    if merge_to_mkv:
        download_opts["merge_output_format"] = "mkv"

    with YoutubeDL(cast(Any, download_opts)) as ydl:
        downloaded = _validate_info(cast(dict[str, Any], ydl.extract_info(url, download=True)))

    if cancel_event is not None and cancel_event.is_set():
        raise LeaseLostError
    path = _find_media_file(temp_dir)
    return DownloadResult(path=path, metadata=metadata, info=downloaded)
