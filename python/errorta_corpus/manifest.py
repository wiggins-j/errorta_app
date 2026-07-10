"""Per-corpus manifest persisted at ~/.errorta/corpora/{name}/manifest.json."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from . import corpus_dir


@dataclass
class FileEntry:
    file_id: str
    original_path: str
    copied_path: str
    sha256: str
    size_bytes: int
    mime_ext: str
    status: str = "queued"  # queued | extracting | chunking | embedding | ready | failed | removed
    error: Optional[str] = None
    chunk_count: int = 0
    chunk_ids: list[str] = field(default_factory=list)
    token_count: int = 0
    ingested_at: Optional[str] = None
    progress: float = 0.0


_LOCKS: dict[str, threading.Lock] = {}


def _lock_for(name: str) -> threading.Lock:
    if name not in _LOCKS:
        _LOCKS[name] = threading.Lock()
    return _LOCKS[name]


def manifest_path(name: str) -> Path:
    return corpus_dir(name) / "manifest.json"


def load_manifest(name: str) -> dict[str, FileEntry]:
    p = manifest_path(name)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except Exception:
        return {}
    out: dict[str, FileEntry] = {}
    for fid, entry in raw.get("files", {}).items():
        out[fid] = FileEntry(**entry)
    return out


def save_manifest(name: str, files: dict[str, FileEntry]) -> None:
    p = manifest_path(name)
    payload = {"name": name, "files": {fid: asdict(e) for fid, e in files.items()}}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(p)


def upsert_entry(name: str, entry: FileEntry) -> None:
    with _lock_for(name):
        files = load_manifest(name)
        files[entry.file_id] = entry
        save_manifest(name, files)


def update_status(
    name: str,
    file_id: str,
    *,
    status: Optional[str] = None,
    error: Optional[str] = None,
    chunk_count: Optional[int] = None,
    chunk_ids: Optional[list[str]] = None,
    token_count: Optional[int] = None,
    ingested_at: Optional[str] = None,
    progress: Optional[float] = None,
) -> Optional[FileEntry]:
    with _lock_for(name):
        files = load_manifest(name)
        e = files.get(file_id)
        if e is None:
            return None
        if status is not None:
            e.status = status
        if error is not None:
            e.error = error
        if chunk_count is not None:
            e.chunk_count = chunk_count
        if chunk_ids is not None:
            e.chunk_ids = chunk_ids
        if token_count is not None:
            e.token_count = token_count
        if ingested_at is not None:
            e.ingested_at = ingested_at
        if progress is not None:
            e.progress = progress
        files[file_id] = e
        save_manifest(name, files)
        return e


def remove_entry(name: str, file_id: str) -> Optional[FileEntry]:
    with _lock_for(name):
        files = load_manifest(name)
        e = files.pop(file_id, None)
        save_manifest(name, files)
        return e


def find_by_sha256(name: str, sha256: str) -> Optional[FileEntry]:
    for e in load_manifest(name).values():
        if e.sha256 == sha256:
            return e
    return None


def reserve_or_get_duplicate(
    name: str,
    sha256: str,
    new_entry: FileEntry,
    *,
    overwrite: bool,
) -> tuple[Optional[FileEntry], Optional[FileEntry]]:
    """Atomically decide what to do about a SHA-256 match.

    Returns ``(inserted, existing_duplicate)``:
      * If no prior entry exists, inserts ``new_entry`` and returns
        ``(new_entry, None)``.
      * If a prior entry exists and ``overwrite`` is False, returns
        ``(None, existing)`` — caller should drop the upload.
      * If a prior entry exists and ``overwrite`` is True, removes it from
        the manifest, inserts ``new_entry``, and returns
        ``(new_entry, existing)`` so the caller can evict chunks / unlink
        the old copied file.
    """
    with _lock_for(name):
        files = load_manifest(name)
        existing: Optional[FileEntry] = None
        for e in files.values():
            if e.sha256 == sha256:
                existing = e
                break
        if existing is not None and not overwrite:
            return None, existing
        if existing is not None and overwrite:
            files.pop(existing.file_id, None)
        files[new_entry.file_id] = new_entry
        save_manifest(name, files)
        return new_entry, existing
