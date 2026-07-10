"""F015-BACKEND — corpus refresh-preview diff + apply.

Computes a structural diff between the live state of files on disk (relative
to the paths recorded in a corpus manifest) and a prior snapshot. Used by the
``GET /corpus/{name}/refresh-preview`` endpoint to drive a "what would change
if I re-ingested?" UI affordance for F004.

Includes the preview path (``compute_diff``) and the apply path
(``apply_diff``) which mutates the manifest to bring it in line with the
on-disk state. Apply is serialized per-corpus via a threading lock so
concurrent applies for the same corpus do not interleave.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from . import corpus_dir
from .manifest import (
    FileEntry,
    load_manifest,
    reserve_or_get_duplicate,
    update_status,
)

SNAPSHOT_DIRNAME = "refresh-snapshots"


@dataclass
class RefreshDiff:
    added: list[FileEntry] = field(default_factory=list)
    removed: list[FileEntry] = field(default_factory=list)
    updated: list[tuple[FileEntry, FileEntry]] = field(default_factory=list)
    snapshot_at: str = ""
    partial: bool = False


def _utc_iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomic write helper (mirrors errorta_briefs.runner._atomic_write_json)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def sha256_file(path: Path) -> str:
    """Hash a file with SHA-256 in 1MB chunks.

    Exposed as a module-level function so tests can monkeypatch it and
    assert that mtime+size-stable files are NOT re-hashed.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshots_dir(name: str) -> Path:
    return corpus_dir(name) / SNAPSHOT_DIRNAME


def _list_snapshot_files(name: str) -> list[Path]:
    d = snapshots_dir(name)
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix == ".json")


def _load_snapshot(path: Path) -> dict[str, FileEntry]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, FileEntry] = {}
    for fid, entry in raw.get("files", {}).items():
        out[fid] = FileEntry(**entry)
    return out


def _parse_ingested_at(value: Optional[str]) -> Optional[_dt.datetime]:
    if not value:
        return None
    try:
        v = value.replace("Z", "+00:00")
        return _dt.datetime.fromisoformat(v)
    except Exception:
        return None


def _file_mtime(path: Path) -> Optional[_dt.datetime]:
    try:
        ts = path.stat().st_mtime
    except OSError:
        return None
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)


def _resolve_prior(name: str, last_snapshot_at: Optional[str]) -> dict[str, FileEntry]:
    """Find the prior baseline.

    - If ``last_snapshot_at`` matches an existing snapshot file (by stem),
      use it.
    - Otherwise fall back to the newest snapshot.
    - If no snapshots exist, use the live manifest as the baseline.
    """
    snapshots = _list_snapshot_files(name)
    if last_snapshot_at:
        for s in snapshots:
            if s.stem == last_snapshot_at:
                return _load_snapshot(s)
        # bogus → fall through to newest
    if snapshots:
        return _load_snapshot(snapshots[-1])
    return load_manifest(name)


def _infer_source_roots(prior: dict[str, FileEntry]) -> list[Path]:
    """Distinct parent directories of every prior entry's original_path."""
    roots: set[Path] = set()
    for e in prior.values():
        try:
            p = Path(e.original_path)
        except Exception:
            continue
        if not p.is_absolute():
            # original_path may be a bare basename (upload route stores
            # the basename). We cannot walk a relative root meaningfully.
            continue
        roots.add(p.parent)
    return sorted(roots)


def compute_diff(
    corpus_name: str, last_snapshot_at: Optional[str] = None
) -> RefreshDiff:
    """Compare the live filesystem to a prior snapshot for ``corpus_name``.

    Persists a fresh snapshot of the current live manifest as a side effect.
    """
    cdir = corpus_dir(corpus_name)
    live_manifest = load_manifest(corpus_name)
    prior = _resolve_prior(corpus_name, last_snapshot_at)

    prior_by_path: dict[str, FileEntry] = {}
    for e in prior.values():
        prior_by_path[e.original_path] = e

    added: list[FileEntry] = []
    removed: list[FileEntry] = []
    updated: list[tuple[FileEntry, FileEntry]] = []

    seen_paths: set[str] = set()
    for original_path, entry in prior_by_path.items():
        seen_paths.add(original_path)
        fp = Path(original_path)
        if not fp.is_absolute() or not fp.exists():
            removed.append(entry)
            continue
        try:
            st = fp.stat()
        except OSError:
            removed.append(entry)
            continue
        size = st.st_size
        mtime = _dt.datetime.fromtimestamp(st.st_mtime, tz=_dt.timezone.utc)
        ingested = _parse_ingested_at(entry.ingested_at)

        size_changed = size != entry.size_bytes
        mtime_changed = ingested is None or mtime > ingested

        if not size_changed and not mtime_changed:
            # cheap pre-check passed → do NOT re-hash
            continue

        digest = sha256_file(fp)
        if digest != entry.sha256:
            new_entry = FileEntry(
                file_id=entry.file_id,
                original_path=entry.original_path,
                copied_path=entry.copied_path,
                sha256=digest,
                size_bytes=size,
                mime_ext=entry.mime_ext,
                status=entry.status,
                error=entry.error,
                chunk_count=entry.chunk_count,
                chunk_ids=list(entry.chunk_ids),
                token_count=entry.token_count,
                ingested_at=entry.ingested_at,
                progress=entry.progress,
            )
            updated.append((entry, new_entry))

    # Walk inferred source roots looking for files added since the snapshot.
    for root in _infer_source_roots(prior_by_path):
        if not root.exists() or not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_file():
                continue
            child_str = str(child)
            if child_str in seen_paths:
                continue
            seen_paths.add(child_str)
            try:
                st = child.stat()
            except OSError:
                continue
            digest = sha256_file(child)
            added.append(
                FileEntry(
                    file_id="",
                    original_path=child_str,
                    copied_path="",
                    sha256=digest,
                    size_bytes=st.st_size,
                    mime_ext=child.suffix.lower(),
                    status="candidate",
                )
            )

    snapshot_at = _utc_iso_now()
    snapshot_payload = {
        "name": corpus_name,
        "snapshot_at": snapshot_at,
        "files": {fid: asdict(e) for fid, e in live_manifest.items()},
    }
    _atomic_write_json(cdir / SNAPSHOT_DIRNAME / f"{snapshot_at}.json", snapshot_payload)

    return RefreshDiff(
        added=added,
        removed=removed,
        updated=updated,
        snapshot_at=snapshot_at,
        partial=False,
    )


# ---------------------------------------------------------------------------
# apply_diff — F015-APPLY
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    ingested: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


_APPLY_LOCKS: dict[str, threading.Lock] = {}
_APPLY_LOCKS_GUARD = threading.Lock()


def _apply_lock_for(name: str) -> threading.Lock:
    with _APPLY_LOCKS_GUARD:
        lock = _APPLY_LOCKS.get(name)
        if lock is None:
            lock = threading.Lock()
            _APPLY_LOCKS[name] = lock
        return lock


def _copied_path_for(corpus_name: str, original_name: str) -> Path:
    """Resolve a non-clobbering path under <corpus>/files/ for an apply copy."""
    base = corpus_dir(corpus_name) / "files"
    base.mkdir(parents=True, exist_ok=True)
    candidate = base / original_name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    i = 1
    while True:
        candidate = base / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def _new_file_id() -> str:
    import uuid

    return uuid.uuid4().hex


def apply_diff(corpus_name: str, diff: RefreshDiff) -> ApplyResult:
    """Apply a previously-computed RefreshDiff to the corpus.

    For each FileEntry in ``diff.added``: copy ``original_path`` to a fresh
    ``copied_path`` under the corpus files dir, register via
    ``reserve_or_get_duplicate`` (no overwrite), and enqueue for ingest.

    For each FileEntry in ``diff.removed``: evict the file's chunks from the
    vector store, mark the manifest entry as ``status='removed'``, and clear
    ``copied_path``. The entry is RETAINED for audit (soft-delete).

    For each ``(old, new)`` pair in ``diff.updated``: evict the old chunks,
    copy ``new.original_path`` to a fresh copied path, register with
    ``overwrite=True``, and enqueue.

    Per-file ``IOError`` / ``PermissionError`` failures are captured into
    ``ApplyResult.errors`` and do NOT abort the batch.

    Concurrent applies on the same corpus are serialized via a per-corpus
    threading.Lock; different corpora can run in parallel.
    """
    # Lazy import to avoid pipeline ↔ refresh import cycle at module load.
    from .pipeline import enqueue as _enqueue
    from .pipeline import evict_chunks as _evict_chunks

    result = ApplyResult()
    lock = _apply_lock_for(corpus_name)
    with lock:
        # --- added -----------------------------------------------------
        for entry in diff.added:
            src = Path(entry.original_path)
            try:
                if not src.exists():
                    raise IOError(f"source file missing: {src}")
                original_name = src.name
                dst = _copied_path_for(corpus_name, original_name)
                shutil.copy2(src, dst)
                file_id = entry.file_id or _new_file_id()
                new_entry = FileEntry(
                    file_id=file_id,
                    original_path=str(src),
                    copied_path=str(dst),
                    sha256=entry.sha256,
                    size_bytes=entry.size_bytes,
                    mime_ext=entry.mime_ext or src.suffix.lower(),
                    status="queued",
                )
                inserted, _prior = reserve_or_get_duplicate(
                    corpus_name, entry.sha256, new_entry, overwrite=False
                )
                if inserted is None:
                    # Duplicate already in corpus — drop the copy, no error.
                    try:
                        dst.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                _enqueue(corpus_name, file_id)
                result.ingested.append(file_id)
            except (IOError, OSError, PermissionError) as e:
                result.errors.append((str(entry.original_path), str(e)))

        # --- removed ---------------------------------------------------
        for entry in diff.removed:
            try:
                _evict_chunks(corpus_name, entry.file_id, list(entry.chunk_ids))
                # Soft-delete: keep the manifest row, mark status='removed',
                # clear copied_path so the file row no longer points at an
                # on-disk copy (which we leave alone — apply does not unlink
                # user-facing files; the disk file is the user's source).
                update_status(
                    corpus_name,
                    entry.file_id,
                    status="removed",
                    progress=0.0,
                    error="",
                )
                # Also clear copied_path on the entry. update_status doesn't
                # expose copied_path, so do a direct manifest tweak.
                from .manifest import load_manifest as _lm, save_manifest as _sm
                from .manifest import _lock_for as _lf  # type: ignore

                with _lf(corpus_name):
                    files = _lm(corpus_name)
                    e = files.get(entry.file_id)
                    if e is not None:
                        e.copied_path = ""
                        files[entry.file_id] = e
                        _sm(corpus_name, files)
                result.removed.append(entry.file_id)
            except (IOError, OSError, PermissionError) as e:
                result.errors.append((str(entry.original_path), str(e)))

        # --- updated ---------------------------------------------------
        for old, new in diff.updated:
            try:
                _evict_chunks(corpus_name, old.file_id, list(old.chunk_ids))
                src = Path(new.original_path)
                if not src.exists():
                    raise IOError(f"source file missing: {src}")
                original_name = src.name
                dst = _copied_path_for(corpus_name, original_name)
                shutil.copy2(src, dst)
                file_id = old.file_id
                new_entry = FileEntry(
                    file_id=file_id,
                    original_path=str(src),
                    copied_path=str(dst),
                    sha256=new.sha256,
                    size_bytes=new.size_bytes,
                    mime_ext=new.mime_ext or src.suffix.lower(),
                    status="queued",
                )
                reserve_or_get_duplicate(
                    corpus_name, new.sha256, new_entry, overwrite=True
                )
                _enqueue(corpus_name, file_id)
                result.updated.append(file_id)
            except (IOError, OSError, PermissionError) as e:
                result.errors.append((str(new.original_path), str(e)))

    return result


def apply_result_to_dict(result: ApplyResult) -> dict:
    return {
        "ingested": list(result.ingested),
        "removed": list(result.removed),
        "updated": list(result.updated),
        "errors": [{"path": p, "message": m} for (p, m) in result.errors],
    }


def diff_from_dict(payload: dict) -> RefreshDiff:
    """Inverse of ``diff_to_dict`` for JSON-body apply requests."""
    def _entry(d: dict) -> FileEntry:
        return FileEntry(**d)

    added = [_entry(e) for e in payload.get("added", [])]
    removed = [_entry(e) for e in payload.get("removed", [])]
    updated_raw = payload.get("updated", [])
    updated: list[tuple[FileEntry, FileEntry]] = []
    for pair in updated_raw:
        updated.append((_entry(pair["old"]), _entry(pair["new"])))
    return RefreshDiff(
        added=added,
        removed=removed,
        updated=updated,
        snapshot_at=payload.get("snapshot_at", ""),
        partial=bool(payload.get("partial", False)),
    )


def diff_to_dict(diff: RefreshDiff) -> dict:
    """Serialize a RefreshDiff for JSON response."""
    return {
        "added": [asdict(e) for e in diff.added],
        "removed": [asdict(e) for e in diff.removed],
        "updated": [
            {"old": asdict(old), "new": asdict(new)} for (old, new) in diff.updated
        ],
        "snapshot_at": diff.snapshot_at,
        "partial": diff.partial,
    }
