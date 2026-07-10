"""F010 export planner — pure planning, no file copying."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ExportFile:
    src_path: Path
    dest_path: Path
    size_bytes: int
    sha256_hex: Optional[str] = None


@dataclass
class ExportPlan:
    files: list[ExportFile] = field(default_factory=list)
    total_size_bytes: int = 0
    dest_paths: dict[str, Path] = field(default_factory=dict)
    corpora_included: list[str] = field(default_factory=list)


def _default_errorta_home() -> Path:
    from errorta_app.paths import errorta_home
    return errorta_home()


def planner(
    target_dir: Path,
    corpora_list: list[str],
    errorta_home: Optional[Path] = None,
    include_models: bool = False,
) -> ExportPlan:
    """Plan an export of the named corpora into target_dir/Errorta/corpora/{name}/files/.

    Pure planning — does not copy any bytes. Reads each corpus manifest.json from
    (errorta_home or ~/.errorta)/corpora/{name}/manifest.json and constructs
    ExportFile entries pointing at the existing on-disk copied_path.

    Raises:
        FileNotFoundError: if a named corpus has no manifest on disk.
        NotImplementedError: if include_models is True (reserved for a future slice).
    """
    if include_models:
        raise NotImplementedError(
            "include_models is reserved for a future F010 slice and is not yet supported"
        )

    home = errorta_home if errorta_home is not None else _default_errorta_home()
    target_dir = Path(target_dir)

    plan = ExportPlan()
    export_root = target_dir / "Errorta" / "corpora"

    for corpus_name in corpora_list:
        manifest_path = home / "corpora" / corpus_name / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(
                f"Corpus manifest not found for '{corpus_name}': {manifest_path}"
            )

        try:
            raw = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as e:
            raise FileNotFoundError(
                f"Corpus manifest for '{corpus_name}' is not valid JSON: {e}"
            ) from e

        plan.corpora_included.append(corpus_name)
        corpus_dest_root = export_root / corpus_name / "files"
        plan.dest_paths[corpus_name] = corpus_dest_root

        for _fid, entry in raw.get("files", {}).items():
            copied_path = entry.get("copied_path")
            if not copied_path:
                continue
            src = Path(copied_path)
            filename = src.name
            dest = corpus_dest_root / filename
            size_bytes = int(entry.get("size_bytes") or 0)
            sha256_hex = entry.get("sha256")
            plan.files.append(
                ExportFile(
                    src_path=src,
                    dest_path=dest,
                    size_bytes=size_bytes,
                    sha256_hex=sha256_hex,
                )
            )
            plan.total_size_bytes += size_bytes

    return plan
