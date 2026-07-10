"""Local store for F035 agent-context capsules."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .schema import AgentContextCapsule, AgentContextDelta, _validate_capsule_id


class AgentContextStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.capsules_dir = self.root / "capsules"
        self.deltas_dir = self.root / "deltas"
        self.artifacts_dir = self.root / "artifacts"
        self.capsules_dir.mkdir(parents=True, exist_ok=True)
        self.deltas_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.jsonl"

    def write_capsule(self, capsule: AgentContextCapsule) -> Path:
        path = self.capsules_dir / f"{capsule.capsule_id}.json"
        data = capsule.to_dict()
        _atomic_json(path, data)
        self._append_index({
            "kind": "capsule",
            "capsule_id": capsule.capsule_id,
            "parent_id": capsule.parent_id,
            "created_at": capsule.created_at,
            "canonical_sha256": data["digest"]["canonical_sha256"],
        })
        return path

    def read_capsule(self, capsule_id: str) -> AgentContextCapsule:
        _validate_capsule_id(capsule_id)
        path = self.capsules_dir / f"{capsule_id}.json"
        assert path.resolve().parent == self.capsules_dir.resolve(), "path traversal"
        return AgentContextCapsule.from_dict(json.loads(path.read_text()))

    def write_delta(self, delta: AgentContextDelta) -> Path:
        path = self.deltas_dir / f"{delta.capsule_id}.json"
        _atomic_json(path, delta.to_dict())
        self._append_index({
            "kind": "delta",
            "capsule_id": delta.capsule_id,
            "parent_id": delta.parent_id,
            "created_at": delta.created_at,
        })
        return path

    def read_delta(self, capsule_id: str) -> AgentContextDelta:
        _validate_capsule_id(capsule_id)
        path = self.deltas_dir / f"{capsule_id}.json"
        assert path.resolve().parent == self.deltas_dir.resolve(), "path traversal"
        return AgentContextDelta.from_dict(json.loads(path.read_text()))

    def list_capsules(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in sorted(self.capsules_dir.glob("*.json")):
            try:
                c = AgentContextCapsule.from_dict(json.loads(path.read_text()))
            except Exception:
                continue
            data = c.to_dict()
            out.append({
                "capsule_id": c.capsule_id,
                "kind": c.kind,
                "parent_id": c.parent_id,
                "created_at": c.created_at,
                "task_title": c.task.get("title"),
                "canonical_sha256": data["digest"]["canonical_sha256"],
            })
        return sorted(out, key=lambda x: str(x["created_at"]), reverse=True)

    def materialize(self, capsule_id: str) -> AgentContextCapsule:
        if (self.capsules_dir / f"{capsule_id}.json").exists():
            return self.read_capsule(capsule_id)
        delta = self.read_delta(capsule_id)
        parent = self.materialize(delta.parent_id)
        data = parent.to_dict(include_digest=False)
        _apply_changes(data, delta.changes)
        data["capsule_id"] = delta.capsule_id
        data["parent_id"] = delta.parent_id
        data["created_at"] = delta.created_at
        return AgentContextCapsule.from_dict(data)

    def metadata_for_diagnostics(self) -> dict[str, Any]:
        capsules = self.list_capsules()
        return {
            "capsule_count": len(capsules),
            "capsules": capsules[:50],
        }

    def _append_index(self, item: dict[str, Any]) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        with self.index_path.open("a") as f:
            f.write(json.dumps(item, sort_keys=True) + "\n")


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.chmod(0o600)
    os.replace(tmp, path)


def _apply_changes(target: dict[str, Any], changes: dict[str, Any]) -> None:
    added = changes.get("added") or {}
    for key, value in added.items():
        if isinstance(target.get(key), list) and isinstance(value, list):
            target[key].extend(value)
        elif isinstance(target.get(key), dict) and isinstance(value, dict):
            target[key].update(value)
        else:
            target[key] = value
    changed = changes.get("changed") or {}
    for key, value in changed.items():
        target[key] = value
    for key in changes.get("removed") or []:
        target.pop(str(key), None)


__all__ = ["AgentContextStore"]
