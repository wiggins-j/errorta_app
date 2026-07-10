"""Run-local artifact capture for ToolRunner."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .types import RunnerArtifactRef


class RunnerArtifactStore:
    def __init__(self, *, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def run_dir(self, *, run_id: str, request_id: str) -> Path:
        path = self._root / _safe_segment(run_id) / _safe_segment(request_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_bytes(
        self,
        *,
        run_id: str,
        request_id: str,
        name: str,
        data: bytes,
        kind: str,
    ) -> RunnerArtifactRef:
        target_dir = self.run_dir(run_id=run_id, request_id=request_id)
        target = target_dir / _safe_filename(name)
        tmp = target.with_suffix(f"{target.suffix}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        digest = hashlib.sha256(data).hexdigest()
        return RunnerArtifactRef(
            kind=kind,
            path=str(target.relative_to(self._root)),
            sha256=digest,
            bytes=len(data),
            metadata={"name": target.name},
        )


def _safe_segment(value: str) -> str:
    safe = "".join(ch for ch in value if ch.isalnum() or ch in {"-", "_"})
    if not safe:
        raise ValueError("empty_artifact_path_segment")
    return safe


def _safe_filename(value: str) -> str:
    safe = "".join(ch for ch in value if ch.isalnum() or ch in {"-", "_", "."})
    if not safe or safe in {".", ".."}:
        raise ValueError("empty_artifact_filename")
    return safe


__all__ = ["RunnerArtifactStore"]
