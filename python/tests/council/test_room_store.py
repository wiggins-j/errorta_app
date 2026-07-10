"""F031-01 — atomic room store CRUD + index + delete + clone."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from errorta_council import paths as council_paths
from errorta_council.room_store import (
    RevisionConflict,
    RoomNotFound,
    RoomStore,
)
from errorta_council.schema import CouncilRoom


def _store() -> RoomStore:
    return RoomStore(rooms_dir=council_paths.rooms_dir(),
                     deleted_dir=council_paths.deleted_rooms_dir())


def test_create_writes_one_room_file(tmp_errorta_home: Path, sample_room: CouncilRoom) -> None:
    store = _store()
    saved = store.create(sample_room)
    assert saved.revision == 1
    on_disk = council_paths.rooms_dir() / f"{sample_room.id}.json"
    assert on_disk.is_file()
    raw = json.loads(on_disk.read_text())
    assert raw["id"] == sample_room.id
    assert raw["format_version"] == 1


def test_update_increments_revision(tmp_errorta_home: Path, sample_room: CouncilRoom) -> None:
    store = _store()
    store.create(sample_room)
    updated = store.update(sample_room.id, expected_revision=1,
                           mutate=lambda r: {**r, "name": "Renamed"})
    assert updated.revision == 2
    assert updated.name == "Renamed"


def test_update_stale_revision_raises(tmp_errorta_home: Path, sample_room: CouncilRoom) -> None:
    store = _store()
    store.create(sample_room)
    store.update(sample_room.id, expected_revision=1,
                 mutate=lambda r: {**r, "name": "First Edit"})
    with pytest.raises(RevisionConflict):
        store.update(sample_room.id, expected_revision=1,
                     mutate=lambda r: {**r, "name": "Conflicting Edit"})


def test_delete_moves_file_to_deleted(tmp_errorta_home: Path, sample_room: CouncilRoom) -> None:
    store = _store()
    store.create(sample_room)
    store.delete(sample_room.id)
    assert not (council_paths.rooms_dir() / f"{sample_room.id}.json").exists()
    assert (council_paths.deleted_rooms_dir() / f"{sample_room.id}.json").exists()


def test_list_rebuilds_index_when_missing(tmp_errorta_home: Path, sample_room: CouncilRoom) -> None:
    store = _store()
    store.create(sample_room)
    (council_paths.rooms_dir() / "index.json").unlink()
    summaries = store.list()
    assert len(summaries) == 1
    assert summaries[0].id == sample_room.id
    assert (council_paths.rooms_dir() / "index.json").is_file()


def test_list_skips_corrupt_room_file(tmp_errorta_home: Path, sample_room: CouncilRoom) -> None:
    store = _store()
    store.create(sample_room)
    bad = council_paths.rooms_dir() / "garbage.json"
    bad.write_text("{not valid json")
    (council_paths.rooms_dir() / "index.json").unlink()
    summaries = store.list()
    # Corrupt file is skipped, valid room still listed.
    ids = [s.id for s in summaries]
    assert sample_room.id in ids
    assert "garbage" not in ids


def test_leftover_tmp_is_ignored(tmp_errorta_home: Path, sample_room: CouncilRoom) -> None:
    store = _store()
    store.create(sample_room)
    stray = council_paths.rooms_dir() / f"{sample_room.id}.json.tmp"
    stray.write_text("partial")
    (council_paths.rooms_dir() / "index.json").unlink()
    summaries = store.list()
    assert [s.id for s in summaries] == [sample_room.id]
    assert stray.exists()  # Store does not touch it; recovery can later.


def test_get_unknown_raises(tmp_errorta_home: Path) -> None:
    with pytest.raises(RoomNotFound):
        _store().get("nope")


def test_clone_creates_new_id_and_revision_one(
    tmp_errorta_home: Path, sample_room: CouncilRoom
) -> None:
    store = _store()
    store.create(sample_room)
    cloned = store.clone(sample_room.id, new_id="room-2", new_name="Copy")
    assert cloned.id == "room-2"
    assert cloned.name == "Copy"
    assert cloned.revision == 1
    # Original still present.
    assert store.get(sample_room.id).id == sample_room.id
