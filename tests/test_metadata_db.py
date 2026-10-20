from __future__ import annotations

from pathlib import Path

from media_loader.metadata_db import MetadataDatabase


async def test_metadata_database_upsert_and_persist(tmp_path: Path) -> None:
    path = tmp_path / "metadata.sqlite3"
    database = MetadataDatabase(path)
    await database.upsert(
        "source1",
        "https://example.com/video",
        {"title": "First", "tags": ["a"], "description": "text"},
    )
    first = await database.get("source1")
    assert first is not None
    assert first["title"] == "First"
    await database.upsert(
        "source1",
        "https://example.com/video",
        {"title": "Updated", "tags": ["b"]},
    )
    updated = await database.get("source1")
    assert updated == {"title": "Updated", "tags": ["b"]}
    assert await database.get("missing") is None
    await database.close()

    reopened = MetadataDatabase(path)
    reopened_record = await reopened.get("source1")
    assert reopened_record is not None
    assert reopened_record["title"] == "Updated"
    await reopened.close()
