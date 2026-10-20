from __future__ import annotations

from media_loader.canonical import (
    canonicalize_url,
    host_key,
    request_key,
    source_key,
)


def test_youtube_url_variants_share_source_key() -> None:
    urls = [
        "https://youtu.be/dQw4w9WgXcQ?t=30",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&si=abc",
        "https://youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "https://m.youtube.com/live/dQw4w9WgXcQ",
    ]
    canonical = {canonicalize_url(url) for url in urls}
    assert canonical == {"https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
    assert len({source_key(value) for value in canonical}) == 1


def test_generic_url_normalization() -> None:
    first = canonicalize_url("HTTPS://Example.COM:443/video?b=2&a=1&utm_source=x#frag")
    second = canonicalize_url("https://example.com/video?a=1&b=2")
    assert first == second
    assert first == "https://example.com/video?a=1&b=2"


def test_request_key_depends_on_format_and_quality() -> None:
    base = canonicalize_url("https://example.com/video")
    assert request_key(base, "video", "mp4", None) != request_key(base, "video", "mkv", None)
    assert request_key(base, "video", "mp4", "720p") != request_key(base, "audio", "mp3", None)


def test_host_key() -> None:
    assert host_key("https://www.youtube.com/watch?v=abc") == "youtube.com"
    assert host_key("https://Example.com/video") == "example.com"
