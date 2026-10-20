from __future__ import annotations

from pathlib import Path
from typing import Any

from media_loader.config import Settings
from media_loader.transcode import MediaInfo, build_output_plan


def _info(
    *,
    video_codec: str | None = "h264",
    audio_codec: str | None = "aac",
    width: int | None = 1920,
    height: int | None = 1080,
) -> MediaInfo:
    return MediaInfo(
        path=Path("/tmp/source.mkv"),
        has_video=video_codec is not None,
        has_audio=audio_codec is not None,
        width=width,
        height=height,
        video_codec=video_codec,
        audio_codec=audio_codec,
        duration=60.0,
        format_name="matroska",
    )


def test_mkv_source_is_copied(settings: Settings) -> None:
    plan = build_output_plan(
        settings,
        _info(),
        "video",
        "mkv",
        None,
        Path("/tmp/out.mkv"),
    )
    assert plan.copy_source is True
    assert plan.suffix == "mkv"
    assert plan.actual_quality == "1080p"


def test_mkv_lower_quality_transcodes(settings: Settings) -> None:
    plan = build_output_plan(
        settings,
        _info(),
        "video",
        "mkv",
        "720p",
        Path("/tmp/out.mkv"),
    )
    assert plan.copy_source is False
    assert plan.needs_scale is True
    assert plan.command is not None
    assert "scale=-2:720:flags=lanczos,setsar=1" in plan.command
    assert "libx264" in plan.command
    assert plan.actual_quality == "720p"


def test_mp4_compatible_streams_are_remuxed(settings: Settings) -> None:
    plan = build_output_plan(
        settings,
        _info(),
        "video",
        "mp4",
        None,
        Path("/tmp/out.mp4"),
    )
    assert plan.command is not None
    assert plan.command[plan.command.index("-c:v") + 1] == "copy"
    assert plan.command[plan.command.index("-c:a") + 1] == "copy"
    assert "+faststart" in plan.command
    assert plan.stage == "remuxing"


def test_mp4_incompatible_streams_are_transcoded(settings: Settings) -> None:
    plan = build_output_plan(
        settings,
        _info(video_codec="vp9", audio_codec="opus"),
        "video",
        "mp4",
        None,
        Path("/tmp/out.mp4"),
    )
    assert plan.command is not None
    assert "libx264" in plan.command
    assert "aac" in plan.command
    assert plan.stage == "transcoding"


def test_quality_above_source_is_clamped(settings: Settings) -> None:
    plan = build_output_plan(
        settings,
        _info(width=640, height=360),
        "video",
        "mkv",
        "1080p",
        Path("/tmp/out.mkv"),
    )
    assert plan.copy_source is True
    assert plan.actual_quality == "360p"
    assert plan.quality_clamped is True


def test_audio_formats(settings: Settings) -> None:
    mp3 = build_output_plan(
        settings,
        _info(),
        "audio",
        "mp3",
        None,
        Path("/tmp/out.mp3"),
    )
    assert mp3.suffix == "mp3"
    assert mp3.command is not None
    assert "libmp3lame" in mp3.command

    opus = build_output_plan(
        settings,
        _info(audio_codec="opus"),
        "audio",
        "original",
        None,
        Path("/tmp/out.opus"),
    )
    assert opus.suffix == "opus"
    assert opus.command is not None
    assert opus.command[opus.command.index("-c:a") + 1] == "copy"
    assert opus.stage == "extracting"


def test_audio_request_without_audio_raises(settings: Settings) -> None:
    import pytest

    from media_loader.errors import UnsupportedMediaError

    with pytest.raises(UnsupportedMediaError):
        build_output_plan(
            settings,
            _info(audio_codec=None),
            "audio",
            "mp3",
            None,
            Path("/tmp/out.mp3"),
        )


def _make_media(path: Path) -> None:
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )


def test_probe_and_remux_with_ffmpeg(settings: Settings, tmp_path: Path) -> None:
    import pytest

    pytest.importorskip("shutil")
    import shutil

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg is not installed")
    source = tmp_path / "source.mp4"
    _make_media(source)
    import asyncio

    from media_loader.transcode import probe_media, remux_to_mkv

    media = asyncio.run(probe_media(source, settings))
    assert media.has_video is True
    assert media.has_audio is True
    assert media.width == 320
    assert media.height == 240
    assert media.video_codec == "h264"
    assert media.audio_codec == "aac"
    destination = tmp_path / "source.mkv"
    asyncio.run(remux_to_mkv(source, destination, settings))
    assert destination.is_file()


def test_run_ffmpeg_and_failure(settings: Settings, tmp_path: Path) -> None:
    import asyncio
    import shutil

    import pytest

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    source = tmp_path / "source.mp4"
    _make_media(source)
    from media_loader.errors import DownloadFailedError
    from media_loader.transcode import _parse_ffmpeg_time, run_ffmpeg

    assert _parse_ffmpeg_time("00:01:02.5") == 62.5
    assert _parse_ffmpeg_time("bad") is None
    output = tmp_path / "copy.mp4"
    asyncio.run(
        run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-c",
                "copy",
                "-progress",
                "pipe:1",
                "-nostats",
                str(output),
            ],
            duration=1.0,
        )
    )
    assert output.is_file()
    with pytest.raises(DownloadFailedError):
        asyncio.run(
            run_ffmpeg(
                ["ffmpeg", "-y", "-i", str(tmp_path / "missing.mp4"), str(output)],
                duration=1.0,
            )
        )


def test_format_plan_branches(settings: Settings) -> None:
    m4a = build_output_plan(
        settings,
        _info(),
        "audio",
        "m4a",
        None,
        Path("/tmp/out.m4a"),
    )
    assert m4a.suffix == "m4a"
    assert m4a.command is not None
    assert m4a.command[m4a.command.index("-c:a") + 1] == "copy"

    m4a_transcode = build_output_plan(
        settings,
        _info(audio_codec="opus"),
        "audio",
        "m4a",
        None,
        Path("/tmp/out.m4a"),
    )
    assert m4a_transcode.command is not None
    assert m4a_transcode.command[m4a_transcode.command.index("-c:a") + 1] == "aac"

    original_unknown = build_output_plan(
        settings,
        _info(audio_codec="weird"),
        "audio",
        "original",
        None,
        Path("/tmp/out"),
    )
    assert original_unknown.suffix == "mka"
    assert original_unknown.command is not None
    assert original_unknown.command[-1].endswith("out")

    mp4_lower = build_output_plan(
        settings,
        _info(width=1920, height=1080),
        "video",
        "mp4",
        "720p",
        Path("/tmp/out.mp4"),
    )
    assert mp4_lower.needs_scale is True
    assert mp4_lower.command is not None
    assert "scale=-2:720:flags=lanczos,setsar=1" in mp4_lower.command
    assert mp4_lower.command[mp4_lower.command.index("-c:a") + 1] == "copy"

    no_audio = build_output_plan(
        settings,
        _info(audio_codec=None),
        "video",
        "mp4",
        None,
        Path("/tmp/out.mp4"),
    )
    assert no_audio.command is not None
    assert "-an" in no_audio.command


def test_probe_invalid_file(settings: Settings, tmp_path: Path) -> None:
    import asyncio
    import shutil

    import pytest

    from media_loader.errors import DownloadFailedError
    from media_loader.transcode import probe_media

    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe is not installed")
    path = tmp_path / "bad.mp4"
    path.write_text("not a video")
    with pytest.raises(DownloadFailedError):
        asyncio.run(probe_media(path, settings))


def test_run_ffmpeg_progress(settings: Settings, tmp_path: Path) -> None:
    import asyncio
    import shutil

    import fakeredis
    import pytest

    from media_loader.progress import ProgressReporter
    from media_loader.store import job_progress_key
    from media_loader.transcode import run_ffmpeg

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    source = tmp_path / "source.mp4"
    _make_media(source)
    output = tmp_path / "scaled.mp4"
    redis: Any = fakeredis.FakeRedis(decode_responses=True)
    reporter = ProgressReporter(settings, client=redis, throttle_seconds=0)
    asyncio.run(
        run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-vf",
                "scale=-2:120",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "copy",
                "-progress",
                "pipe:1",
                "-nostats",
                str(output),
            ],
            duration=1.0,
            reporter=reporter,
            request_key="job1",
            stage="transcoding",
        )
    )
    assert output.is_file()
    values = redis.hgetall(job_progress_key("job1"))
    assert values["stage"] == "finalizing"
    assert float(values["percent"]) == 100.0
    reporter.close()
