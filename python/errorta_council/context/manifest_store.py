"""Persist ContextManifest under ${ERRORTA_HOME}/council/context-manifests/{manifest_id}.json.

Keyed solely by manifest_id (hex). Never contains raw payload text.
Manifest format_version = 1.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any


class ContextManifestStore:
    def __init__(self, *, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def write(self, manifest: Any) -> Path:
        if hasattr(manifest, "__dataclass_fields__"):
            data = asdict(manifest)
        else:
            data = dict(manifest)
        manifest_id = data["manifest_id"]
        path = self._root / f"{manifest_id}.json"
        path.write_text(json.dumps(data, sort_keys=True, indent=2, default=_jsonable))
        return path

    def read(self, manifest_id: str) -> dict[str, Any]:
        path = self._root / f"{manifest_id}.json"
        return json.loads(path.read_text())

    def list_by_run(self, run_id: str) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self._root.glob("*.json")):
            try:
                d = json.loads(p.read_text())
            except Exception:
                continue
            if d.get("run_id") == run_id:
                out.append(d)
        return out


def _jsonable(o):
    if hasattr(o, "__dataclass_fields__"):
        return asdict(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


__all__ = ["ContextManifestStore"]
