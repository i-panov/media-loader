from __future__ import annotations

from media_loader.quality import (
    output_dimensions,
    plan_quality,
    quality_height,
    scale_filter,
    source_height,
)


def test_source_height_uses_short_side() -> None:
    assert source_height(1920, 1080) == 1080
    assert source_height(1080, 1920) == 1080


def test_quality_is_clamped_without_upscale() -> None:
    plan = plan_quality("720p", 640, 360)
    assert plan.source_quality == "360p"
    assert plan.actual_quality == "360p"
    assert plan.quality_clamped is True
    assert plan.needs_scale is False


def test_lower_quality_requests_scale() -> None:
    plan = plan_quality("720p", 1920, 1080)
    assert plan.actual_quality == "720p"
    assert plan.quality_clamped is False
    assert plan.needs_scale is True
    assert plan.target_height == 720


def test_output_dimensions_landscape_and_portrait() -> None:
    assert output_dimensions(1920, 1080, 720) == (1280, 720)
    assert output_dimensions(1080, 1920, 720) == (720, 1280)


def test_scale_filter_orientation() -> None:
    assert scale_filter(1920, 1080, 720).startswith("scale=-2:720")
    assert scale_filter(1080, 1920, 720).startswith("scale=720:-2")


def test_quality_height() -> None:
    assert quality_height("1080p") == 1080
