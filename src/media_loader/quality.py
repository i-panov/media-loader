from __future__ import annotations

import re
from dataclasses import dataclass

_QUALITY_RE = re.compile(r"^(\d+)p$")


def quality_height(label: str) -> int:
    match = _QUALITY_RE.match(label.strip().lower())
    if not match:
        raise ValueError(f"invalid quality label: {label!r}")
    return int(match.group(1))


def quality_label(height: int) -> str:
    return f"{height}p"


def source_height(width: int | None, height: int | None) -> int | None:
    if not width or not height:
        return None
    return min(width, height)


@dataclass(frozen=True)
class QualityPlan:
    requested: str | None
    source_quality: str | None
    actual_quality: str | None
    source_height: int | None
    target_height: int | None
    quality_clamped: bool
    needs_scale: bool


def plan_quality(requested: str | None, width: int | None, height: int | None) -> QualityPlan:
    source_h = source_height(width, height)
    if source_h is None:
        return QualityPlan(requested, None, None, None, None, False, False)

    source_label = quality_label(source_h)
    if requested is None:
        return QualityPlan(None, source_label, source_label, source_h, source_h, False, False)

    requested_h = quality_height(requested)
    if requested_h >= source_h:
        return QualityPlan(
            requested,
            source_label,
            source_label,
            source_h,
            source_h,
            requested_h > source_h,
            False,
        )
    return QualityPlan(
        requested,
        source_label,
        quality_label(requested_h),
        source_h,
        requested_h,
        False,
        True,
    )


def output_dimensions(
    width: int,
    height: int,
    target_height: int | None,
) -> tuple[int, int]:
    if target_height is None or target_height >= min(width, height):
        return width, height
    shorter = min(width, height)
    if shorter == 0:
        return width, height
    scale = target_height / shorter
    out_w = max(2, round(width * scale / 2) * 2)
    out_h = max(2, round(height * scale / 2) * 2)
    return out_w, out_h


def scale_filter(width: int, height: int, target_height: int) -> str:
    if width >= height:
        return f"scale=-2:{target_height}:flags=lanczos,setsar=1"
    return f"scale={target_height}:-2:flags=lanczos,setsar=1"
