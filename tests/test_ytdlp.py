from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import fakeredis
import pytest
from yt_dlp.utils import DownloadError

from media_loader import ytdlp
from media_loader.config import Settings
from media_loader.errors import UnsupportedMediaError
from media_loader.progress import ProgressReporter
from media_loader.ytdlp import (
    _find_media_file,
    _format_selector,
    _validate_info,
    describe_media,
    download_source,
    is_retryable_error,
    metadata_to_dict,
    probe_source_metadata,
)


def _info() -> dict[str, Any]:
    return {
        "id": "abc",
        "title": "Test video",
        "description": "Description",
        "duration": 12.5,
        "thumbnail": "https://example.com/t.jpg",
        "uploader": "Uploader",
        "channel": "Channel",
        "upload_date": "20260101",
        "view_count": 10,
        "like_count": 2,
        "tags": ["tag"],
        "categories": ["Category"],
        "chapters": [{"title": "Intro", "start_time": 0, "end_time": 5}],
        "subtitles": {"en": [{"url": "https://example.com/sub"}]},
        "automatic_captions": {"ru": [{"url": "https://example.com/auto"}]},
        "formats": [
            {
                "format_id": "v240",
                "vcodec": "h264",
                "acodec": "none",
                "height": 240,
                "width": 320,
                "tbr": 100,
                "ext": "mp4",
            },
            {
                "format_id": "v720",
                "vcodec": "h264",
                "acodec": "none",
                "height": 720,
                "width": 1280,
                "tbr": 200,
                "ext": "mp4",
            },
            {
                "format_id": "a",
                "vcodec": "none",
                "acodec": "aac",
                "abr": 128,
                "ext": "m4a",
            },
        ],
    }


class FakeYoutubeDL:
    last_options: ClassVar[dict[str, Any]] = {}
    info: ClassVar[dict[str, Any]] = _info()
    downloaded_file: ClassVar[str] = "video.mp4"
    raise_on_extract: ClassVar[Exception | None] = None

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        type(self).last_options = options

    def __enter__(self) -> FakeYoutubeDL:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
        del url
        if self.raise_on_extract:
            raise self.raise_on_extract
        if download:
            home = Path(self.options["paths"]["home"])
            path = home / self.downloaded_file
            path.write_bytes(b"x")
            return self.info
        return self.info


def test_describe_media_rich_fields() -> None:
    metadata = describe_media(_info())
    assert metadata.title == "Test video"
    assert metadata.description == "Description"
    assert metadata.has_video is True
    assert metadata.has_audio is True
    assert metadata.max_quality == "720p"
    assert metadata.available_qualities == ("240p", "720p")
    assert metadata.video_codec == "h264"
    assert metadata.audio_codec == "aac"
    assert metadata.details["uploader"] == "Uploader"
    assert metadata.details["tags"] == ["tag"]
    assert metadata.details["chapters"][0]["title"] == "Intro"
    assert metadata.details["subtitles"] == ["en"]
    assert len(metadata.details["formats"]) == 3
    assert "url" not in metadata.details["formats"][0]


def test_describe_audio_only_and_direct_video() -> None:
    audio = {
        "title": "Audio",
        "formats": [{"format_id": "a", "vcodec": "none", "acodec": "opus"}],
    }
    metadata = describe_media(audio)
    assert metadata.has_video is False
    assert metadata.has_audio is True
    assert metadata.max_quality is None
    direct = {"title": "Direct", "ext": "mp4"}
    assert describe_media(direct).has_video is True


def test_format_selector() -> None:
    selector, merge = _format_selector(_info())
    assert merge is True
    assert selector.startswith("bv*[protocol^=http]")
    assert "format_note!*=Premium" in selector
    assert selector.endswith("/bv*+ba/b")
    assert _format_selector({"formats": [{"vcodec": "none", "acodec": "aac"}]}) == (
        "bestaudio/best",
        False,
    )
    assert _format_selector({"ext": "mp4"}) == ("best", False)
    assert _format_selector({"ext": "mp3"}) == ("bestaudio/best", False)


def test_validate_info_rejects_playlist_and_live() -> None:
    with pytest.raises(UnsupportedMediaError):
        _validate_info({"_type": "playlist"})
    with pytest.raises(UnsupportedMediaError):
        _validate_info({"is_live": True})
    with pytest.raises(DownloadError):
        _validate_info(None)
    assert _validate_info({"title": "ok"})["title"] == "ok"


def test_retryable_errors() -> None:
    assert is_retryable_error("read timed out") is True
    assert is_retryable_error("connection reset") is True
    assert is_retryable_error("unable to download video data: fragment") is True
    assert is_retryable_error("unsupported url") is False
    assert is_retryable_error("http error 404") is False
    assert is_retryable_error("some unexpected extractor error") is True


def test_metadata_to_dict() -> None:
    payload = metadata_to_dict(describe_media(_info()))
    assert payload["title"] == "Test video"
    assert payload["source_quality"] == "720p"
    assert payload["available_qualities"] == ["240p", "720p"]
    assert payload["description"] == "Description"


def test_find_media_file(tmp_path: Path) -> None:
    (tmp_path / "video.mp4").write_bytes(b"video")
    (tmp_path / "video.part").write_bytes(b"x" * 100)
    (tmp_path / ".tmp").mkdir()
    (tmp_path / ".tmp" / "hidden.mp4").write_bytes(b"hidden")
    assert _find_media_file(tmp_path).name == "video.mp4"
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(DownloadError):
        _find_media_file(empty)


def test_probe_and_download_with_fake_ytdlp(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ytdlp, "YoutubeDL", FakeYoutubeDL)
    FakeYoutubeDL.info = _info()
    FakeYoutubeDL.raise_on_extract = None
    FakeYoutubeDL.downloaded_file = "video.mp4"
    metadata, info = probe_source_metadata(
        "https://example.com/video",
        None,
        settings,
        player_client="tv_embedded",
    )
    assert metadata.title == "Test video"
    assert info["formats"]
    assert FakeYoutubeDL.last_options["skip_download"] is True
    assert FakeYoutubeDL.last_options["extractor_args"] == {
        "youtube": {"player_client": ["tv_embedded"]}
    }

    redis = fakeredis.FakeRedis(decode_responses=True)
    reporter = ProgressReporter(settings, client=redis)
    result = download_source(
        "https://example.com/video",
        tmp_path,
        None,
        settings,
        reporter,
        "source1",
        fast_fail=True,
        player_client="tv_embedded",
    )
    assert result.path.name == "video.mp4"
    assert result.metadata.title == "Test video"
    assert FakeYoutubeDL.last_options["extractor_retries"] == 0
    assert FakeYoutubeDL.last_options["concurrent_fragment_downloads"] == 4

    proxied = download_source(
        "https://example.com/video",
        tmp_path,
        "socks5://127.0.0.1:1080",
        settings,
        reporter,
        "source1",
        fast_fail=False,
    )
    assert proxied.path.name == "video.mp4"
    assert FakeYoutubeDL.last_options["fragment_retries"] == 5
    assert FakeYoutubeDL.last_options["concurrent_fragment_downloads"] == 1
    reporter.close()


def test_probe_playlist_raises(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ytdlp, "YoutubeDL", FakeYoutubeDL)
    FakeYoutubeDL.info = {"_type": "playlist"}
    FakeYoutubeDL.raise_on_extract = None
    with pytest.raises(UnsupportedMediaError):
        probe_source_metadata("https://example.com/playlist", None, settings)
    FakeYoutubeDL.info = _info()
