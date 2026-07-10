"""F031-01 — mutable room JSON store.

Atomic temp-write + rename so an interrupted write cannot leave a
half-written room as valid. Optimistic concurrency via ``revision``;
stale updates raise ``RevisionConflict``. Deletes move the room file to
``rooms/deleted/`` (architecture-spec OQ#4: deletes do not cascade to
runs because runs carry snapshots).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .schema import FORMAT_VERSION, CouncilRoom, UnsupportedFormatVersion


class RoomNotFound(LookupError):
    pass


class RevisionConflict(RuntimeError):
    def __init__(self, room_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"revision conflict for room {room_id}: expected {expected}, on-disk {actual}"
        )
        self.room_id = room_id
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class RoomSummary:
    id: str
    name: str
    updated_at: str
    revision: int
    status_hint: str


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    fd = os.open(str(tmp), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


class RoomStore:
    def __init__(self, rooms_dir: Path, deleted_dir: Path) -> None:
        self._rooms_dir = rooms_dir
        self._deleted_dir = deleted_dir
        self._index_path = rooms_dir / "index.json"

    # ---- internal helpers -------------------------------------------------

    def _room_path(self, room_id: str) -> Path:
        return self._rooms_dir / f"{room_id}.json"

    def _read_room_file(self, path: Path) -> CouncilRoom | None:
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return CouncilRoom.from_dict(raw)
        except (UnsupportedFormatVersion, KeyError, TypeError, ValueError):
            return None

    def _rebuild_index(self) -> list[RoomSummary]:
        summaries: list[RoomSummary] = []
        for child in sorted(self._rooms_dir.iterdir()):
            if not child.is_file():
                continue
            if child.suffix != ".json":
                continue
            if child.name == "index.json":
                continue
            room = self._read_room_file(child)
            if room is None:
                continue
            summaries.append(
                RoomSummary(
                    id=room.id, name=room.name, updated_at=room.updated_at,
                    revision=room.revision, status_hint=room.status_hint,
                )
            )
        _atomic_write_json(
            self._index_path,
            {
                "format_version": FORMAT_VERSION,
                "rooms": [
                    {"id": s.id, "name": s.name, "updated_at": s.updated_at,
                     "revision": s.revision, "status_hint": s.status_hint}
                    for s in summaries
                ],
            },
        )
        return summaries

    # ---- public API -------------------------------------------------------

    def create(self, room: CouncilRoom) -> CouncilRoom:
        path = self._room_path(room.id)
        if path.exists():
            raise FileExistsError(f"room {room.id} already exists")
        _atomic_write_json(path, room.to_dict())
        self._rebuild_index()
        return room

    def get(self, room_id: str) -> CouncilRoom:
        path = self._room_path(room_id)
        if not path.exists():
            raise RoomNotFound(room_id)
        room = self._read_room_file(path)
        if room is None:
            raise RoomNotFound(room_id)
        return room

    def update(
        self,
        room_id: str,
        *,
        expected_revision: int,
        mutate: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> CouncilRoom:
        current = self.get(room_id)
        if current.revision != expected_revision:
            raise RevisionConflict(room_id, expected_revision, current.revision)
        raw = current.to_dict()
        new_raw = mutate(raw)
        new_raw["revision"] = current.revision + 1
        new_raw["created_at"] = current.created_at  # preserve
        new_raw["format_version"] = FORMAT_VERSION
        new_room = CouncilRoom.from_dict(new_raw)
        _atomic_write_json(self._room_path(room_id), new_room.to_dict())
        self._rebuild_index()
        return new_room

    def delete(self, room_id: str) -> None:
        path = self._room_path(room_id)
        if not path.exists():
            raise RoomNotFound(room_id)
        target = self._deleted_dir / path.name
        os.replace(path, target)
        self._rebuild_index()

    def list(self) -> list[RoomSummary]:
        if not self._index_path.exists():
            return self._rebuild_index()
        try:
            raw = json.loads(self._index_path.read_text())
        except (OSError, json.JSONDecodeError):
            return self._rebuild_index()
        return [
            RoomSummary(
                id=r["id"], name=r["name"], updated_at=r["updated_at"],
                revision=int(r["revision"]), status_hint=r.get("status_hint", "draft"),
            )
            for r in raw.get("rooms", [])
        ]

    def clone(self, room_id: str, *, new_id: str, new_name: str) -> CouncilRoom:
        src = self.get(room_id)
        raw = src.to_dict()
        raw["id"] = new_id
        raw["name"] = new_name
        raw["revision"] = 1
        raw["preset_id"] = src.id
        new_room = CouncilRoom.from_dict(raw)
        return self.create(new_room)
