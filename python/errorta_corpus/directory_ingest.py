"""Helper for ingesting an existing on-disk directory into a corpus.

Used by F005 (folder watcher) and F007 (welcome corpus install) so they can
funnel files into the same F004 pipeline that drag-and-drop uploads use.

The semantics mirror the upload endpoint:
  * Each supported file is hashed (SHA-256), copied under the corpus' files/
    directory, reserved in the manifest via ``reserve_or_get_duplicate``,
    and enqueued on the F004 pipeline worker.
  * Duplicate hashes are skipped (no overwrite).
  * Unsupported extensions are skipped.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from . import corpus_dir, validate_corpus_name
from .manifest import FileEntry, reserve_or_get_duplicate
from .pipeline import copied_path_for, enqueue, new_file_id


@dataclass
class DirectoryIngestResult:
    corpus_name: str
    enqueued: list[str] = field(default_factory=list)  # file_ids enqueued
    duplicates: list[str] = field(default_factory=list)  # relative paths
    skipped: list[str] = field(default_factory=list)  # relative paths
    errors: list[str] = field(default_factory=list)


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_files(root: Path) -> Iterable[Path]:
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            yield p


def ingest_directory(
    source_root: Path,
    *,
    name: str,
    overwrite_duplicates: bool = False,
    supported: Optional[set[str]] = None,
) -> DirectoryIngestResult:
    """Walk ``source_root``, copy supported files into ``name`` and enqueue."""
    validate_corpus_name(name)
    source_root = Path(source_root)
    if supported is None:
        # Local import to avoid a hard dep at module import time.
        from errorta_extract.registry import supported_extensions

        supported_exts = set(supported_extensions())
    else:
        supported_exts = set(supported)

    result = DirectoryIngestResult(corpus_name=name)
    if not source_root.is_dir():
        result.errors.append(f"source not a directory: {source_root}")
        return result

    files_root = (corpus_dir(name) / "files").resolve()

    for src in _iter_files(source_root):
        rel = src.relative_to(source_root)
        ext = src.suffix.lower()
        if ext not in supported_exts:
            result.skipped.append(str(rel))
            continue

        try:
            digest = _sha256_of(src)
            size_bytes = src.stat().st_size
        except OSError as exc:
            result.errors.append(f"{rel}: {exc}")
            continue

        target = copied_path_for(name, src.name)
        try:
            resolved = target.resolve()
            resolved.relative_to(files_root)
        except (OSError, ValueError):
            result.errors.append(f"{rel}: invalid target path")
            continue

        # Copy bytes into the corpus dir.
        try:
            with src.open("rb") as inp, target.open("wb") as out:
                for chunk in iter(lambda: inp.read(1 << 20), b""):
                    out.write(chunk)
        except OSError as exc:
            result.errors.append(f"{rel}: copy failed: {exc}")
            continue

        file_id = new_file_id()
        new_entry = FileEntry(
            file_id=file_id,
            original_path=str(src),
            copied_path=str(target),
            sha256=digest,
            size_bytes=size_bytes,
            mime_ext=ext,
            status="queued",
        )
        inserted, prior = reserve_or_get_duplicate(
            name, digest, new_entry, overwrite=overwrite_duplicates
        )
        if inserted is None:
            # Duplicate; drop the freshly written copy.
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            result.duplicates.append(str(rel))
            continue

        if prior is not None:
            # Overwrite: clean up prior copied file (chunks are managed by pipeline).
            try:
                Path(prior.copied_path).unlink(missing_ok=True)
            except OSError:
                pass

        enqueue(name, file_id)
        result.enqueued.append(file_id)

    return result
