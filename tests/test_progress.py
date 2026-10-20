from __future__ import annotations

import threading
from typing import Any

import fakeredis
import pytest

from media_loader.config import Settings
from media_loader.errors import LeaseLostError
from media_loader.progress import DownloadProgressTracker, ProgressReporter
from media_loader.store import job_progress_key, source_progress_key


def test_progress_reporter_writes_and_throttles(settings: Settings) -> None:
    redis: Any = fakeredis.FakeRedis(decode_responses=True)
    reporter = ProgressReporter(settings, client=redis)
    reporter.source("source1", stage="downloading", percent=10)
    first = redis.hgetall(source_progress_key("source1"))
    assert first["stage"] == "downloading"
    assert first["percent"] == "10"
    reporter.source("source1", stage="downloading", percent=20)
    assert redis.hget(source_progress_key("source1"), "percent") == "10"
    reporter.source("source1", stage="downloading", percent=30, _force=True)
    assert redis.hget(source_progress_key("source1"), "percent") == "30"
    reporter.job("job1", stage="transcoding", percent=50)
    assert redis.hget(job_progress_key("job1"), "percent") == "50"
    reporter.clear_source("source1")
    assert not redis.exists(source_progress_key("source1"))
    reporter.close()


def test_download_progress_tracker_aggregates(settings: Settings) -> None:
    redis: Any = fakeredis.FakeRedis(decode_responses=True)
    reporter = ProgressReporter(settings, client=redis, throttle_seconds=0)
    tracker = DownloadProgressTracker(reporter, "source1")
    info = {
        "requested_formats": [
            {"format_id": "v", "filesize": 100},
            {"format_id": "a", "filesize": 50},
        ],
        "format_id": "v",
    }
    tracker.hook(
        {
            "status": "downloading",
            "info_dict": info,
            "format_id": "v",
            "downloaded_bytes": 50,
            "total_bytes": 100,
            "speed": 1000,
            "eta": 5,
        }
    )
    values = redis.hgetall(source_progress_key("source1"))
    assert float(values["percent"]) == 33.33
    tracker.hook(
        {
            "status": "finished",
            "info_dict": {**info, "format_id": "v"},
            "format_id": "v",
            "downloaded_bytes": 100,
        }
    )
    tracker.hook(
        {
            "status": "finished",
            "info_dict": {**info, "format_id": "a"},
            "format_id": "a",
            "downloaded_bytes": 50,
        }
    )
    values = redis.hgetall(source_progress_key("source1"))
    assert float(values["percent"]) == 100.0
    reporter.close()


def test_download_progress_tracker_stops_on_lease_loss(settings: Settings) -> None:
    redis = fakeredis.FakeRedis(decode_responses=True)
    reporter = ProgressReporter(settings, client=redis)
    cancel_event = threading.Event()
    cancel_event.set()
    tracker = DownloadProgressTracker(reporter, "source1", cancel_event)
    with pytest.raises(LeaseLostError):
        tracker.hook({"status": "downloading", "info_dict": {}})
    reporter.close()
