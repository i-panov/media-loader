from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from media_loader.config import Settings
from media_loader.errors import InvalidRequestError

VIDEO_FORMATS = frozenset({"mp4", "mkv"})
AUDIO_FORMATS = frozenset({"m4a", "mp3", "opus", "original"})
MAX_FORMAT = "max"

MediaType = Literal["video", "audio"]


class VideoRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    url: HttpUrl
    audio_only: bool = False
    metadata_only: bool = False
    output_format: str | None = Field(default=None, alias="format")
    quality: str | None = None


@dataclass(frozen=True)
class ResolvedRequest:
    media_type: MediaType
    output_format: str
    quality: str | None


def resolve_request(settings: Settings, request: VideoRequest) -> ResolvedRequest:
    raw_format = (request.output_format or "").strip().lower()
    quality = (request.quality or "").strip().lower() or None

    if request.audio_only:
        if quality is not None:
            raise InvalidRequestError("quality is only supported for video requests")
        output_format = raw_format or settings.default_audio_format.lower()
        if output_format not in AUDIO_FORMATS:
            allowed = ", ".join(sorted(AUDIO_FORMATS))
            raise InvalidRequestError(
                f"unsupported audio format {output_format!r}; allowed: {allowed}"
            )
        return ResolvedRequest("audio", output_format, None)

    output_format = raw_format or settings.default_video_format.lower()
    if output_format not in VIDEO_FORMATS:
        allowed = ", ".join(sorted(VIDEO_FORMATS))
        raise InvalidRequestError(f"unsupported video format {output_format!r}; allowed: {allowed}")
    if quality is not None and quality not in settings.quality_level_set:
        allowed = ", ".join(sorted(settings.quality_level_set))
        raise InvalidRequestError(f"unsupported quality {quality!r}; allowed: {allowed}")
    return ResolvedRequest("video", output_format, quality)
