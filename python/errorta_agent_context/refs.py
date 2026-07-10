"""Reference parsing and deterministic summaries for F035."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RefSummary:
    uri: str
    ok: bool
    summary: str
    sha256: str | None = None
    reason: str | None = None


class ReferenceResolver:
    def __init__(self, *, repo_root: Path, errorta_home: Path) -> None:
        self._repo_root = Path(repo_root)
        self._errorta_home = Path(errorta_home)

    def summarize(self, uri: str) -> RefSummary:
        if uri.startswith("file://"):
            return self._file(uri[len("file://"):])
        if uri.startswith("spec://"):
            return self._spec(uri[len("spec://"):])
        if uri.startswith("run://council/"):
            return self._run(uri[len("run://council/"):])
        if uri.startswith("manifest://context/"):
            return self._manifest(uri[len("manifest://context/"):])
        if uri.startswith("manifest://"):
            return self._manifest(uri[len("manifest://"):])
        if uri.startswith("log://") or uri.startswith("cmd://"):
            return RefSummary(uri=uri, ok=False, summary="", reason="artifact_not_available")
        return RefSummary(uri=uri, ok=False, summary="", reason="unknown_scheme")

    def _file(self, raw: str) -> RefSummary:
        path_part = raw.split("#", 1)[0]
        path = (self._repo_root / path_part).resolve()
        try:
            path.relative_to(self._repo_root.resolve())
        except ValueError:
            return RefSummary(uri=f"file://{raw}", ok=False, summary="", reason="outside_repo")
        try:
            data = path.read_bytes()
        except OSError:
            return RefSummary(uri=f"file://{raw}", ok=False, summary="", reason="not_found")
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        summary = f"{path_part}: {len(lines)} lines, {len(data)} bytes"
        return RefSummary(uri=f"file://{raw}", ok=True, summary=summary, sha256=_sha_bytes(data))

    def _spec(self, ident: str) -> RefSummary:
        matches = sorted((self._repo_root / "docs" / "specs").glob(f"{ident}*.md"))
        if not matches:
            return RefSummary(uri=f"spec://{ident}", ok=False, summary="", reason="not_found")
        data = matches[0].read_bytes()
        return RefSummary(
            uri=f"spec://{ident}",
            ok=True,
            summary=f"{matches[0].name}: {len(data)} bytes",
            sha256=_sha_bytes(data),
        )

    def _run(self, raw: str) -> RefSummary:
        run_id = raw.split("#", 1)[0]
        path = self._errorta_home / "council" / "runs" / f"{run_id}.jsonl"
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return RefSummary(uri=f"run://council/{raw}", ok=False, summary="", reason="not_found")
        counts: dict[str, int] = {}
        for line in lines:
            try:
                typ = json.loads(line).get("type", "unknown")
            except Exception:
                typ = "invalid"
            counts[str(typ)] = counts.get(str(typ), 0) + 1
        return RefSummary(
            uri=f"run://council/{raw}",
            ok=True,
            summary=f"run {run_id}: {len(lines)} events; types={counts}",
            sha256=_sha_bytes(path.read_bytes()),
        )

    def _manifest(self, manifest_id: str) -> RefSummary:
        path = self._errorta_home / "council" / "context-manifests" / f"{manifest_id}.json"
        try:
            data = json.loads(path.read_text())
        except OSError:
            return RefSummary(uri=f"manifest://context/{manifest_id}", ok=False, summary="", reason="not_found")
        except json.JSONDecodeError:
            return RefSummary(uri=f"manifest://context/{manifest_id}", ok=False, summary="", reason="invalid_json")
        summary = (
            f"manifest {manifest_id}: context={data.get('effective_context_access')} "
            f"transcript={data.get('effective_transcript_access')} "
            f"sources={data.get('source_counts', {})}"
        )
        return RefSummary(
            uri=f"manifest://context/{manifest_id}",
            ok=True,
            summary=summary,
            sha256=_sha_bytes(path.read_bytes()),
        )


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


__all__ = ["ReferenceResolver", "RefSummary"]
