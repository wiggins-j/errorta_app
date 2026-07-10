"""F010 streaming sha256 + manifest verification tests."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from errorta_export import sha256_file, verify_checksums


def test_sha256_file_matches_hashlib_5mb(tmp_path: Path) -> None:
    # Deterministic 5 MB payload.
    payload = (b"errorta-f010-streaming-sha256-test-" * 1024)
    # Bring it to ~5 MB.
    while len(payload) < 5 * 1024 * 1024:
        payload += payload
    payload = payload[: 5 * 1024 * 1024]

    p = tmp_path / "blob.bin"
    p.write_bytes(payload)

    expected = hashlib.sha256(payload).hexdigest()
    actual = sha256_file(p)
    assert actual == expected
    assert actual == actual.lower()


def test_verify_checksums_detects_mutation_and_missing(tmp_path: Path) -> None:
    base = tmp_path / "usb"
    files_dir = base / "files"
    files_dir.mkdir(parents=True)

    a = files_dir / "a.bin"
    b = files_dir / "b.bin"
    a_bytes = b"hello-world-aaaa" * 1024
    b_bytes = b"hello-world-bbbb" * 1024
    a.write_bytes(a_bytes)
    b.write_bytes(b_bytes)

    a_sha = hashlib.sha256(a_bytes).hexdigest()
    b_sha = hashlib.sha256(b_bytes).hexdigest()

    manifest = {
        "version": "1",
        "files": {
            "files/a.bin": {"sha256": a_sha, "size_bytes": len(a_bytes)},
            "files/b.bin": {"sha256": b_sha, "size_bytes": len(b_bytes)},
            "files/missing.bin": {"sha256": "0" * 64, "size_bytes": 0},
        },
    }
    manifest_path = base / "export-manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    # Both intact, missing is False.
    result = verify_checksums(manifest_path, base)
    assert result["files/a.bin"] is True
    assert result["files/b.bin"] is True
    assert result["files/missing.bin"] is False

    # Mutate one byte of b.
    with open(b, "r+b") as f:
        f.seek(0)
        f.write(b"X")
        # Ensure flush so re-hash sees change.
        f.flush()
        os.fsync(f.fileno())

    result2 = verify_checksums(manifest_path, base)
    assert result2["files/a.bin"] is True
    assert result2["files/b.bin"] is False
    assert result2["files/missing.bin"] is False
