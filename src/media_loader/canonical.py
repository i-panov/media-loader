from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from media_loader.schemas import MAX_FORMAT

_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "igshid",
}
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}


def _is_tracking(name: str) -> bool:
    lower = name.lower()
    return lower.startswith("utm_") or lower in _TRACKING_PARAMS


def _youtube_id(host: str, path: str, query: dict[str, str]) -> str | None:
    path = path.rstrip("/")
    if host == "youtu.be":
        candidate = path.lstrip("/").split("/", 1)[0]
        return candidate or None
    if host in _YOUTUBE_HOSTS:
        if path == "/watch":
            return query.get("v") or None
        for prefix in ("/shorts/", "/embed/", "/live/", "/v/"):
            if path.startswith(prefix):
                candidate = path[len(prefix) :].split("/", 1)[0]
                return candidate or None
    return None


def canonicalize_url(raw: str) -> str:
    parsed = urlsplit(raw.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("only http/https URLs are supported")

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("URL has no host")

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query_dict = dict(query_pairs)
    video_id = _youtube_id(host, parsed.path, query_dict)
    if video_id:
        return f"https://www.youtube.com/watch?v={quote(video_id, safe='-_.')}"

    port = parsed.port
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]" + (f":{port}" if port else "")

    filtered = [(key, value) for key, value in query_pairs if not _is_tracking(key)]
    filtered.sort(key=lambda item: (item[0], item[1]))
    query = urlencode(filtered, doseq=True)
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


def source_key(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:32]


def request_key(
    canonical_url: str,
    media_type: str,
    output_format: str,
    quality: str | None,
) -> str:
    material = f"{canonical_url}|{media_type}:{output_format}|{quality or MAX_FORMAT}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def host_key(canonical_url: str) -> str:
    host = (urlsplit(canonical_url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host
