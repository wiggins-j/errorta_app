"""F008-BUNDLE — Brief bundle export.

Packs a brief's persisted artifacts plus a snapshot of its corpus files into
a portable, verifiable ``.tar.gz`` archive.

Layout inside the archive::

    brief-{brief_id}-{timestamp}/
        brief.md
        brief-manifest.json
        collect-state.json          # optional, only if present on disk
        dedup-index.json            # optional
        run-extras.json             # optional
        run-logs/*                  # optional, full directory copy
        corpus-manifest.json        # snapshot of corpus manifest.json
        bundle-manifest.json        # this module's verification manifest
        corpus/files/...            # FileEntry.copied_path payloads

``build_bundle`` streams every file through a SHA-256 hasher in 4 MiB chunks
(mirroring ``errorta_export.copy.copy_with_progress``) so the per-file shas
recorded in ``bundle-manifest.json`` come straight from the bytes that hit the
archive — no re-read pass, no drift risk.

``dry_run=True`` walks every candidate file and returns counts + sizes, but
writes nothing (no staging dir, no tar.gz, no temp file). Real runs assemble
the staging directory inside a ``TemporaryDirectory`` and ``os.replace`` the
finished archive into ``dest_path`` atomically. On any failure the temp file
is unlinked before the exception escapes.
"""
from __future__ import annotations

import hashlib
import json
import os
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


_CHUNK = 4 * 1024 * 1024


class BundleError(Exception):
    """Raised for any bundle-build failure (missing brief, unreadable file, etc)."""


@dataclass
class BundleFileRecord:
    """One row in ``bundle-manifest.json``'s ``files`` array."""

    path: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size_bytes": self.size_bytes}


@dataclass
class BundleResult:
    """Return value from :func:`build_bundle`.

    ``dest_path`` is the resolved destination — populated even on dry runs so
    callers can echo where the archive *would* have been written.
    ``sha256_hex`` is empty on dry runs (no bytes to hash) and the final
    archive sha on real runs.
    """

    brief_id: str
    dest_path: Path
    file_count: int = 0
    total_size_bytes: int = 0
    sha256_hex: str = ""
    files: list[BundleFileRecord] = field(default_factory=list)
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stream_sha256_and_copy(
    src: Path, dst: Optional[Path], *, chunk: int = _CHUNK
) -> tuple[str, int]:
    """Stream ``src`` through SHA-256, optionally writing to ``dst``.

    Returns ``(sha256_hex, size_bytes)``. ``dst=None`` is the dry-run mode:
    bytes are hashed but never written.
    """
    hasher = hashlib.sha256()
    total = 0
    if dst is not None:
        dst.parent.mkdir(parents=True, exist_ok=True)
    src_fh = open(src, "rb")
    try:
        dst_fh = open(dst, "wb") if dst is not None else None
        try:
            while True:
                buf = src_fh.read(chunk)
                if not buf:
                    break
                hasher.update(buf)
                total += len(buf)
                if dst_fh is not None:
                    dst_fh.write(buf)
        finally:
            if dst_fh is not None:
                dst_fh.close()
    finally:
        src_fh.close()
    return hasher.hexdigest(), total


def _sha256_file(path: Path, *, chunk: int = _CHUNK) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _gather_brief_artifacts(brief_dir: Path) -> list[tuple[Path, str]]:
    """Return the list of ``(absolute_source, archive_relative)`` for brief artifacts.

    Includes ``brief.md`` (required), the per-brief JSON state files (optional),
    and the contents of ``run-logs/`` (optional). The first element is always
    ``brief.md`` so callers can surface a clear error if it's missing.
    """
    out: list[tuple[Path, str]] = []
    brief_md = brief_dir / "brief.md"
    if not brief_md.exists():
        raise BundleError(f"brief.md missing under {brief_dir}")
    out.append((brief_md, "brief.md"))

    for name in ("brief-manifest.json", "collect-state.json", "dedup-index.json", "run-extras.json"):
        p = brief_dir / name
        if p.exists():
            out.append((p, name))

    run_logs = brief_dir / "run-logs"
    if run_logs.exists() and run_logs.is_dir():
        for entry in sorted(run_logs.rglob("*")):
            if entry.is_file():
                rel = entry.relative_to(brief_dir).as_posix()
                out.append((entry, rel))
    return out


def _gather_corpus_files(brief_dir: Path) -> tuple[Optional[Path], list[tuple[Path, str]]]:
    """Return ``(manifest_path_or_None, [(src, archive_rel)…])`` for corpus payload.

    The corpus manifest is the per-corpus ``manifest.json`` written by
    ``errorta_corpus.manifest.save_manifest``. Each ``FileEntry.copied_path``
    referenced from it is included under ``corpus/files/<basename>``; entries
    whose copied_path is missing on disk are skipped silently (the brief may
    still be mid-collection or the user may have pruned files).
    """
    manifest_path = brief_dir / "manifest.json"
    files: list[tuple[Path, str]] = []
    if not manifest_path.exists():
        return None, files
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BundleError(f"corpus manifest unreadable: {exc}") from exc
    seen_names: set[str] = set()
    for _fid, entry in (raw.get("files") or {}).items():
        cp = entry.get("copied_path")
        if not cp:
            continue
        src = Path(cp)
        if not src.exists() or not src.is_file():
            continue
        base = src.name
        # Disambiguate if two FileEntries point at distinct files with the
        # same basename: prefix with the file_id when a collision is hit.
        archive_rel = f"corpus/files/{base}"
        if archive_rel in seen_names:
            archive_rel = f"corpus/files/{entry.get('file_id', base)}_{base}"
        seen_names.add(archive_rel)
        files.append((src, archive_rel))
    return manifest_path, files


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_bundle(
    brief_id: str,
    dest_path: Path,
    *,
    dry_run: bool = False,
    progress: Optional[Callable[[str, dict[str, Any]], None]] = None,
    briefs_root: Optional[Path] = None,
) -> BundleResult:
    """Build a portable ``.tar.gz`` bundle for ``brief_id``.

    Parameters
    ----------
    brief_id:
        Corpus-slug identifier the routes layer uses (see
        ``errorta_app.routes.briefs._brief_dir``).
    dest_path:
        Where the finished ``.tar.gz`` should land. Required even for dry
        runs (echoed back on the result so the UI can preview).
    dry_run:
        If True, walk every file, compute counts and per-file shas, but write
        nothing to disk.
    progress:
        Optional callback ``progress(event_name, payload)``. Event names:
        ``"planning"``, ``"file"`` (one per source file), ``"packaging"``,
        ``"verifying"``, ``"done"``.
    briefs_root:
        Override for the brief root directory. Defaults to
        ``errorta_corpus.corpus_root()`` so tests can isolate via HOME.
    """
    from errorta_corpus import corpus_root

    if briefs_root is None:
        briefs_root = corpus_root()
    brief_dir = briefs_root / brief_id
    if not brief_dir.exists() or not (brief_dir / "brief.md").exists():
        raise BundleError(f"brief '{brief_id}' not found at {brief_dir}")

    if progress is not None:
        progress("planning", {"brief_id": brief_id})

    brief_artifacts = _gather_brief_artifacts(brief_dir)
    corpus_manifest_src, corpus_files = _gather_corpus_files(brief_dir)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bundle_root_name = f"brief-{brief_id}-{timestamp}"
    dest_path = Path(dest_path)

    result = BundleResult(brief_id=brief_id, dest_path=dest_path, dry_run=dry_run)

    if dry_run:
        # Walk every candidate file, hash via stream (no writes), record sizes.
        for src, rel in brief_artifacts:
            sha, size = _stream_sha256_and_copy(src, None)
            rec = BundleFileRecord(path=rel, sha256=sha, size_bytes=size)
            result.files.append(rec)
            result.file_count += 1
            result.total_size_bytes += size
            if progress is not None:
                progress("file", {"path": rel, "size_bytes": size})
        if corpus_manifest_src is not None:
            sha, size = _stream_sha256_and_copy(corpus_manifest_src, None)
            result.files.append(BundleFileRecord("corpus-manifest.json", sha, size))
            result.file_count += 1
            result.total_size_bytes += size
            if progress is not None:
                progress("file", {"path": "corpus-manifest.json", "size_bytes": size})
        for src, rel in corpus_files:
            sha, size = _stream_sha256_and_copy(src, None)
            result.files.append(BundleFileRecord(rel, sha, size))
            result.file_count += 1
            result.total_size_bytes += size
            if progress is not None:
                progress("file", {"path": rel, "size_bytes": size})
        if progress is not None:
            progress("packaging", {"dry_run": True})
            progress("done", {"dry_run": True, "file_count": result.file_count})
        return result

    # Real build: stage in a TemporaryDirectory, tar.gz to a tmp file, os.replace.
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_archive: Optional[Path] = None
    try:
        with tempfile.TemporaryDirectory(prefix="errorta-bundle-") as td:
            stage = Path(td) / bundle_root_name
            stage.mkdir(parents=True, exist_ok=True)

            for src, rel in brief_artifacts:
                dst = stage / rel
                sha, size = _stream_sha256_and_copy(src, dst)
                result.files.append(BundleFileRecord(rel, sha, size))
                result.file_count += 1
                result.total_size_bytes += size
                if progress is not None:
                    progress("file", {"path": rel, "size_bytes": size})

            if corpus_manifest_src is not None:
                dst = stage / "corpus-manifest.json"
                sha, size = _stream_sha256_and_copy(corpus_manifest_src, dst)
                result.files.append(BundleFileRecord("corpus-manifest.json", sha, size))
                result.file_count += 1
                result.total_size_bytes += size
                if progress is not None:
                    progress("file", {"path": "corpus-manifest.json", "size_bytes": size})

            for src, rel in corpus_files:
                dst = stage / rel
                sha, size = _stream_sha256_and_copy(src, dst)
                result.files.append(BundleFileRecord(rel, sha, size))
                result.file_count += 1
                result.total_size_bytes += size
                if progress is not None:
                    progress("file", {"path": rel, "size_bytes": size})

            # Write bundle-manifest.json (with placeholder sha256_hex; we fill
            # the final-archive sha after the tar.gz is sealed and re-hashed).
            generated_at = datetime.now(timezone.utc).isoformat()
            manifest_payload: dict[str, Any] = {
                "version": 1,
                "brief_id": brief_id,
                "generated_at": generated_at,
                "file_count": result.file_count,
                "total_size_bytes": result.total_size_bytes,
                "sha256_hex": "",  # final archive sha — filled below
                "files": [r.to_dict() for r in result.files],
            }
            manifest_text = json.dumps(manifest_payload, indent=2, sort_keys=True)
            (stage / "bundle-manifest.json").write_text(manifest_text, encoding="utf-8")

            if progress is not None:
                progress("packaging", {"file_count": result.file_count})

            # tar.gz the staged dir to a sibling tmp file in dest_path.parent.
            fd, tmp_name = tempfile.mkstemp(
                prefix=".errorta-bundle-", suffix=".tar.gz.tmp", dir=str(dest_path.parent)
            )
            os.close(fd)
            tmp_archive = Path(tmp_name)
            with tarfile.open(tmp_archive, "w:gz") as tf:
                tf.add(str(stage), arcname=bundle_root_name)

            if progress is not None:
                progress("verifying", {})

            # Hash the final archive bytes; record as the bundle's overall sha.
            archive_sha = _sha256_file(tmp_archive)
            result.sha256_hex = archive_sha

            os.replace(tmp_archive, dest_path)
            tmp_archive = None

            if progress is not None:
                progress(
                    "done",
                    {
                        "dest_path": str(dest_path),
                        "sha256_hex": archive_sha,
                        "file_count": result.file_count,
                        "total_size_bytes": result.total_size_bytes,
                    },
                )
    except Exception:
        # Atomic cleanup: never leave a partial tar.gz at dest_path.
        if tmp_archive is not None:
            try:
                if tmp_archive.exists():
                    tmp_archive.unlink()
            except OSError:
                pass
        raise

    return result
