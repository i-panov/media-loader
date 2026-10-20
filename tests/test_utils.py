from __future__ import annotations

from datetime import UTC
from pathlib import Path
from zoneinfo import ZoneInfo

from media_loader.utils import (
    atomic_move,
    hardlink_or_copy,
    human_size,
    is_within,
    iso,
    local_display,
    parse_dt,
    proxy_host_port,
    safe_filename,
    utcnow,
)


def test_time_helpers() -> None:
    now = utcnow()
    assert now.tzinfo is UTC
    parsed = parse_dt(iso(now))
    assert parsed is not None
    assert parsed.timestamp() == now.timestamp()
    assert parse_dt(None) is None
    assert parse_dt("not-a-date") is None
    assert local_display(now, ZoneInfo("UTC")) == now.strftime("%Y-%m-%d %H:%M:%S")
    assert local_display(None, ZoneInfo("UTC")) is None
    assert local_display(now, ZoneInfo("Europe/Moscow")) is not None


def test_safe_filename() -> None:
    assert safe_filename("a/b:c", "fallback", ".mp4") == "a_b_c.mp4"
    assert safe_filename("   ", "fallback", ".mkv") == "fallback.mkv"
    long_name = safe_filename("x" * 300, "fallback", ".mp4", max_length=20)
    assert len(long_name) == 20
    assert long_name.endswith(".mp4")


def test_is_within(tmp_path: Path) -> None:
    inside = tmp_path / "a" / "b.txt"
    inside.parent.mkdir()
    inside.write_text("x")
    assert is_within(tmp_path, inside)
    assert not is_within(tmp_path, tmp_path.parent / "outside.txt")


def test_atomic_move_and_hardlink(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("payload")
    destination = tmp_path / "nested" / "dest.txt"
    atomic_move(source, destination)
    assert not source.exists()
    assert destination.read_text() == "payload"

    link = tmp_path / "hardlink.txt"
    hardlink_or_copy(destination, link)
    assert link.read_text() == "payload"
    destination.write_text("changed")
    assert link.read_text() == "changed"


def test_atomic_move_replaces_directory(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("payload")
    destination = tmp_path / "dest"
    destination.mkdir()
    (destination / "old").write_text("old")
    atomic_move(source, destination)
    assert destination.is_file()
    assert destination.read_text() == "payload"


def test_proxy_and_human_size() -> None:
    assert proxy_host_port("socks5://byedpi:1080") == ("byedpi", 1080)
    assert proxy_host_port("socks5://byedpi") == ("byedpi", 1080)
    assert human_size(0).startswith("0.0")
    assert human_size(1024**3).startswith("1.0")
