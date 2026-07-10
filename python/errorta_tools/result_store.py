"""Tool result side store.

Run events carry hashes/provenance only. Raw tool output is persisted here so
the context router can project it into a later member's sealed payload only
when policy allows.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .gateway import ToolCallResult


class ToolResultStore:
    def __init__(self, *, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _run_dir(self, run_id: str) -> Path:
        p = self._root / run_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _path(self, run_id: str, call_id: str) -> Path:
        safe = "".join(ch for ch in call_id if ch.isalnum() or ch in {"-", "_"})
        if not safe:
            raise ValueError("empty_tool_call_id")
        return self._run_dir(run_id) / f"{safe}.json"

    def write(self, *, run_id: str, result: ToolCallResult) -> Path:
        path = self._path(run_id, result.call_id)
        audit = result.audit_projection()
        payload = {
            "format_version": 1,
            "run_id": run_id,
            **audit,
            "content": result.content,
        }
        tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        os.replace(tmp, path)
        return path

    def read(self, *, run_id: str, call_id: str) -> dict[str, Any]:
        return json.loads(self._path(run_id, call_id).read_text())


__all__ = ["ToolResultStore"]
