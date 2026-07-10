"""F010 streaming SHA-256 + manifest verification."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def sha256_file(file_path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Stream-hash a file in chunk_size byte chunks; return lowercase hex digest."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def verify_checksums(manifest_path: Path, base_dir: Path) -> dict[str, bool]:
    """Re-hash each file referenced in the export manifest and compare to recorded sha256.

    Returns a mapping of dest_relpath -> True (match) / False (mismatch, missing,
    or unreadable). Does not raise on I/O errors per-file.
    """
    manifest_path = Path(manifest_path)
    base_dir = Path(base_dir)
    raw = json.loads(manifest_path.read_text())
    files = raw.get("files", {}) or {}
    out: dict[str, bool] = {}
    for relpath, meta in files.items():
        recorded = (meta or {}).get("sha256")
        target = base_dir / relpath
        if not target.exists() or not target.is_file():
            out[relpath] = False
            continue
        try:
            actual = sha256_file(target)
        except OSError:
            out[relpath] = False
            continue
        out[relpath] = bool(recorded) and actual == recorded
    return out
