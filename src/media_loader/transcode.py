from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from media_loader.config import Settings
from media_loader.errors import DownloadFailedError, UnsupportedMediaError
from media_loader.progress import ProgressReporter
from media_loader.quality import (
    output_dimensions,
    plan_quality,
    scale_filter,
)
from media_loader.utils import CONTENT_TYPES

LOGGER = logging.getLogger(__name__)

_ORIGINAL_AUDIO_MAP: dict[str, tuple[str, str, str]] = {
    "aac": ("m4a", "audio/mp4", "mp4"),
    "mp3": ("mp3", "audio/mpeg", "mp3"),
    "opus": ("opus", "audio/ogg", "opus"),
    "vorbis": ("ogg", "audio/ogg", "ogg"),
    "flac": ("flac", "audio/flac", "flac"),
    "ac3": ("ac3", "audio/ac3", "ac3"),
    "eac3": ("eac3", "audio/eac3", "eac3"),
}


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    has_video: bool
    has_audio: bool
    width: int | None
    height: int | None
    video_codec: str | None
    audio_codec: str | None
    duration: float | None
    format_name: str


@dataclass(frozen=True)
class OutputPlan:
    copy_source: bool
    command: list[str] | None
    suffix: str
    content_type: str
    stage: str
    video_codec: str | None
    audio_codec: str | None
    width: int | None
    height: int | None
    source_quality: str | None
    actual_quality: str | None
    quality_clamped: bool
    needs_scale: bool


async def probe_media(path: Path, settings: Settings) -> MediaInfo:
    command = [
        settings.ffprobe_path,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise DownloadFailedError(f"ffprobe failed: {stderr.decode(errors='replace')[-1000:]}")
    try:
        payload = json.loads(stdout.decode())
    except json.JSONDecodeError as exc:
        raise DownloadFailedError("ffprobe returned invalid JSON") from exc

    streams = payload.get("streams") or []
    video_stream = next(
        (item for item in streams if item.get("codec_type") == "video"),
        None,
    )
    audio_stream = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        None,
    )
    width = int(video_stream["width"]) if video_stream and video_stream.get("width") else None
    height = int(video_stream["height"]) if video_stream and video_stream.get("height") else None
    if video_stream and width and height:
        rotation = 0.0
        for side_data in video_stream.get("side_data_list") or []:
            if "rotation" in side_data:
                rotation = float(side_data["rotation"])
                break
        if not rotation:
            rotation = float((video_stream.get("tags") or {}).get("rotate") or 0)
        if abs(round(rotation)) % 180 == 90:
            width, height = height, width

    duration_raw = (payload.get("format") or {}).get("duration")
    if duration_raw is None and video_stream:
        duration_raw = video_stream.get("duration")
    if duration_raw is None and audio_stream:
        duration_raw = audio_stream.get("duration")
    try:
        duration = float(duration_raw) if duration_raw is not None else None
    except (TypeError, ValueError):
        duration = None

    return MediaInfo(
        path=path,
        has_video=video_stream is not None,
        has_audio=audio_stream is not None,
        width=width,
        height=height,
        video_codec=str(video_stream.get("codec_name")) if video_stream else None,
        audio_codec=str(audio_stream.get("codec_name")) if audio_stream else None,
        duration=duration,
        format_name=str((payload.get("format") or {}).get("format_name") or ""),
    )


def _parse_ffmpeg_time(value: str) -> float | None:
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return None


async def run_ffmpeg(
    command: list[str],
    duration: float | None,
    reporter: ProgressReporter | None = None,
    request_key: str | None = None,
    stage: str = "transcoding",
) -> None:
    if reporter and request_key:
        reporter.job(request_key, stage=stage, percent=0, _force=True)
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stderr_task = asyncio.create_task(process.stderr.read())
    last_percent = 0.0
    try:
        async for raw_line in process.stdout:
            line = raw_line.decode(errors="replace").strip()
            if line.startswith("out_time="):
                seconds = _parse_ffmpeg_time(line.split("=", 1)[1])
                if seconds is not None and duration and duration > 0:
                    last_percent = max(0.0, min(100.0, seconds / duration * 100.0))
                    if reporter and request_key:
                        reporter.job(
                            request_key,
                            stage=stage,
                            percent=round(last_percent, 2),
                        )
            elif line == "progress=end":
                last_percent = 100.0
                if reporter and request_key:
                    reporter.job(request_key, stage=stage, percent=100.0, _force=True)
        await process.wait()
        stderr = await stderr_task
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        message = stderr.decode(errors="replace").strip()
        raise DownloadFailedError(f"ffmpeg failed: {message[-2000:] or 'unknown error'}")
    if reporter and request_key:
        reporter.job(request_key, stage="finalizing", percent=100.0, _force=True)


async def remux_to_mkv(source: Path, destination: Path, settings: Settings) -> None:
    command = [
        settings.ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-f",
        "matroska",
        str(destination),
    ]
    await run_ffmpeg(command, duration=None)


def _ffmpeg_prefix(settings: Settings, source: Path) -> list[str]:
    return [
        settings.ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(source),
    ]


def _video_command(
    settings: Settings,
    source: Path,
    output: Path,
    info: MediaInfo,
    suffix: str,
    target_height: int | None,
    video_codec: str,
    audio_codec: str | None,
) -> list[str]:
    command = _ffmpeg_prefix(settings, source)
    command += ["-map", "0:v:0"]
    if info.has_audio:
        command += ["-map", "0:a:0?"]
    if target_height is not None and info.width and info.height:
        command += ["-vf", scale_filter(info.width, info.height, target_height)]
    if video_codec == "copy":
        command += ["-c:v", "copy"]
    else:
        command += [
            "-c:v",
            "libx264",
            "-preset",
            settings.video_preset,
            "-crf",
            str(settings.video_crf),
            "-pix_fmt",
            "yuv420p",
        ]
    if not info.has_audio:
        command += ["-an"]
    elif audio_codec == "copy":
        command += ["-c:a", "copy"]
    elif audio_codec == "aac":
        command += ["-c:a", "aac", "-b:a", settings.audio_aac_bitrate]
    if suffix == "mp4":
        command += ["-movflags", "+faststart", "-f", "mp4"]
    else:
        command += ["-f", "matroska"]
    command += ["-progress", "pipe:1", "-nostats", str(output)]
    return command


def _audio_command(
    settings: Settings,
    source: Path,
    output: Path,
    suffix: str,
    desired_codec: str,
    copy_audio: bool,
) -> list[str]:
    command = _ffmpeg_prefix(settings, source)
    command += ["-map", "0:a:0", "-vn"]
    if copy_audio:
        command += ["-c:a", "copy"]
    elif desired_codec == "aac":
        command += ["-c:a", "aac", "-b:a", settings.audio_aac_bitrate]
    elif desired_codec == "mp3":
        command += ["-c:a", "libmp3lame", "-b:a", settings.audio_mp3_bitrate]
    elif desired_codec == "opus":
        command += ["-c:a", "libopus", "-b:a", settings.audio_opus_bitrate]
    else:
        command += ["-c:a", "copy"]

    if suffix == "m4a":
        command += ["-movflags", "+faststart", "-f", "mp4"]
    elif suffix == "mp3":
        command += ["-f", "mp3"]
    elif suffix == "opus":
        command += ["-f", "opus"]
    elif suffix == "ogg":
        command += ["-f", "ogg"]
    elif suffix == "flac":
        command += ["-f", "flac"]
    elif suffix == "ac3":
        command += ["-f", "ac3"]
    elif suffix == "eac3":
        command += ["-f", "eac3"]
    else:
        command += ["-f", "matroska"]
    command += ["-progress", "pipe:1", "-nostats", str(output)]
    return command


def build_output_plan(
    settings: Settings,
    info: MediaInfo,
    media_type: str,
    output_format: str,
    requested_quality: str | None,
    output_path: Path,
) -> OutputPlan:
    if media_type == "audio":
        if not info.has_audio or not info.audio_codec:
            raise UnsupportedMediaError("source has no audio stream")
        if output_format == "m4a":
            suffix, content_type, codec = "m4a", CONTENT_TYPES["m4a"], "aac"
            copy_audio = info.audio_codec == "aac"
        elif output_format == "mp3":
            suffix, content_type, codec = "mp3", CONTENT_TYPES["mp3"], "mp3"
            copy_audio = info.audio_codec == "mp3"
        elif output_format == "opus":
            suffix, content_type, codec = "opus", CONTENT_TYPES["opus"], "opus"
            copy_audio = info.audio_codec == "opus"
        elif output_format == "original":
            suffix, content_type, _muxer = _ORIGINAL_AUDIO_MAP.get(
                info.audio_codec,
                ("mka", CONTENT_TYPES["mka"], "matroska"),
            )
            codec = info.audio_codec
            copy_audio = True
        else:
            raise UnsupportedMediaError(f"unsupported audio format: {output_format}")
        command = _audio_command(
            settings,
            info.path,
            output_path,
            suffix,
            codec,
            copy_audio,
        )
        stage = "extracting" if copy_audio else "transcoding"
        return OutputPlan(
            copy_source=False,
            command=command,
            suffix=suffix,
            content_type=content_type,
            stage=stage,
            video_codec=None,
            audio_codec=codec,
            width=None,
            height=None,
            source_quality=None,
            actual_quality=None,
            quality_clamped=False,
            needs_scale=False,
        )

    if not info.has_video:
        raise UnsupportedMediaError("source has no video stream")
    plan = plan_quality(requested_quality, info.width, info.height)
    suffix = output_format
    if output_format not in {"mp4", "mkv"}:
        raise UnsupportedMediaError(f"unsupported video format: {output_format}")
    content_type = CONTENT_TYPES[output_format]

    if output_format == "mkv" and not plan.needs_scale:
        return OutputPlan(
            copy_source=True,
            command=None,
            suffix="mkv",
            content_type=content_type,
            stage="remuxing",
            video_codec=info.video_codec,
            audio_codec=info.audio_codec,
            width=info.width,
            height=info.height,
            source_quality=plan.source_quality,
            actual_quality=plan.actual_quality,
            quality_clamped=plan.quality_clamped,
            needs_scale=False,
        )

    out_width, out_height = output_dimensions(
        info.width or 0,
        info.height or 0,
        plan.target_height if plan.needs_scale else None,
    )
    video_codec = "copy" if info.video_codec == "h264" and not plan.needs_scale else "h264"

    if not info.has_audio:
        audio_codec = None
    elif output_format == "mp4":
        audio_codec = "copy" if info.audio_codec == "aac" else "aac"
    else:
        audio_codec = "copy"

    command = _video_command(
        settings,
        info.path,
        output_path,
        info,
        suffix,
        plan.target_height if plan.needs_scale else None,
        video_codec,
        audio_codec,
    )
    encoded = video_codec != "copy" or audio_codec not in {None, "copy"}
    return OutputPlan(
        copy_source=False,
        command=command,
        suffix=suffix,
        content_type=content_type,
        stage="transcoding" if encoded else "remuxing",
        video_codec=info.video_codec if video_codec == "copy" else "h264",
        audio_codec=(
            None if audio_codec is None else info.audio_codec if audio_codec == "copy" else "aac"
        ),
        width=out_width or None,
        height=out_height or None,
        source_quality=plan.source_quality,
        actual_quality=plan.actual_quality,
        quality_clamped=plan.quality_clamped,
        needs_scale=plan.needs_scale,
    )
