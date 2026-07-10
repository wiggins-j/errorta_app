"""F010 export-manifest writer."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .planner import ExportPlan


def _iso_utc_now() -> str:
    # ISO8601 UTC with seconds precision; e.g. 2026-06-08T12:34:56+00:00
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_export_manifest(target_dir: Path, plan: ExportPlan) -> Path:
    """Write export-manifest.json under target_dir describing the planned export.

    Schema:
        {
          "version": "1",
          "exported_at": ISO8601 UTC string,
          "total_size_bytes": int,
          "file_count": int,
          "corpora": [str, ...],
          "files": {
            dest_relpath: {
              "sha256": str | null,
              "size_bytes": int,
              "original_path": str
            }, ...
          }
        }

    Returns the path to the written manifest. Validates the file parses as JSON
    before returning.
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    files_section: dict[str, dict] = {}
    for ef in plan.files:
        try:
            rel = ef.dest_path.relative_to(target_dir)
        except ValueError:
            rel = Path(ef.dest_path.name)
        rel_str = str(rel).replace("\\", "/")
        # F086 producer-side defense-in-depth: never EMIT a key the importer
        # would reject (absolute / traversal). Import-side validation is the
        # load-bearing boundary; this catches a bug in our own exporter.
        if rel_str.startswith("/") or ".." in rel_str.split("/"):
            raise ValueError(f"refusing to emit unsafe manifest key: {rel_str!r}")
        files_section[rel_str] = {
            "sha256": ef.sha256_hex,
            "size_bytes": ef.size_bytes,
            "original_path": str(ef.src_path),
        }

    payload = {
        "version": "1",
        "exported_at": _iso_utc_now(),
        "total_size_bytes": plan.total_size_bytes,
        "file_count": len(plan.files),
        "corpora": list(plan.corpora_included),
        "files": files_section,
    }

    out_path = target_dir / "export-manifest.json"
    text = json.dumps(payload, indent=2, sort_keys=False)
    out_path.write_text(text)

    # Validate it parses back as JSON before returning.
    json.loads(out_path.read_text())
    return out_path
