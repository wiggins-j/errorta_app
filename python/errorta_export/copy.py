"""F010 export: streaming copy with progress, sha256 integrity, idempotency."""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .checksum import sha256_file
from .planner import ExportPlan


class ExportIntegrityError(Exception):
    """Raised when a copied file's sha256 does not match the planner-recorded digest."""

    def __init__(self, dest_path: Path, expected_sha: str, actual_sha: str) -> None:
        super().__init__(
            f"Integrity mismatch for {dest_path}: expected {expected_sha}, got {actual_sha}"
        )
        self.dest_path = dest_path
        self.expected_sha = expected_sha
        self.actual_sha = actual_sha


@dataclass
class CopyResult:
    files_copied: int = 0
    files_skipped: int = 0
    files_failed: list[tuple[Path, str]] = field(default_factory=list)
    bytes_written: int = 0
    bytes_would_write: int = 0
    duration_s: float = 0.0


def copy_with_progress(
    plan: ExportPlan,
    *,
    progress_cb: Optional[Callable[[int, int, int], None]] = None,
    dry_run: bool = False,
    chunk_size: int = 4 * 1024 * 1024,
) -> CopyResult:
    """Copy each file in plan.files to its dest_path, verifying sha256 along the way.

    progress_cb is called as progress_cb(file_idx, bytes_done, size_bytes).
    """
    result = CopyResult()
    start = time.perf_counter()

    try:
        for idx, ef in enumerate(plan.files):
            size_bytes = int(ef.size_bytes or 0)

            if dry_run:
                if not ef.src_path.exists():
                    raise FileNotFoundError(f"Source file not found: {ef.src_path}")
                result.bytes_would_write += size_bytes
                if progress_cb is not None:
                    progress_cb(idx, size_bytes, size_bytes)
                continue

            # Idempotency: skip if dest already matches.
            if ef.dest_path.exists() and ef.sha256_hex:
                try:
                    existing = sha256_file(ef.dest_path)
                except OSError:
                    existing = None
                if existing == ef.sha256_hex:
                    result.files_skipped += 1
                    if progress_cb is not None:
                        progress_cb(idx, size_bytes, size_bytes)
                    continue

            partial = ef.dest_path.with_suffix(ef.dest_path.suffix + ".partial")

            try:
                ef.dest_path.parent.mkdir(parents=True, exist_ok=True)
                hasher = hashlib.sha256()
                bytes_done = 0
                with open(ef.src_path, "rb") as src, open(partial, "wb") as dst:
                    while True:
                        chunk = src.read(chunk_size)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        dst.write(chunk)
                        bytes_done += len(chunk)
                        if progress_cb is not None:
                            progress_cb(idx, bytes_done, size_bytes)

                # If no chunks were read (empty file), still emit at least one progress event.
                if bytes_done == 0 and progress_cb is not None:
                    progress_cb(idx, 0, size_bytes)

                actual = hasher.hexdigest()
                if ef.sha256_hex and actual != ef.sha256_hex:
                    try:
                        partial.unlink()
                    except OSError:
                        pass
                    raise ExportIntegrityError(ef.dest_path, ef.sha256_hex, actual)

                os.replace(partial, ef.dest_path)
                result.files_copied += 1
                result.bytes_written += bytes_done
            except ExportIntegrityError:
                raise
            except OSError as e:
                try:
                    if partial.exists():
                        partial.unlink()
                except OSError:
                    pass
                result.files_failed.append((ef.dest_path, str(e)))
                continue
    finally:
        result.duration_s = time.perf_counter() - start

    return result
