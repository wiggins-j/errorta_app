"""F086 Slice A — malicious-bundle path-traversal regression tests.

These assert that a crafted manifest key / corpus name / brief id cannot make
the importer read or write outside the staging/target root, and that a rejection
never leaks the sha256 of an out-of-tree file (the disclosure oracle). The
hash-absence assertion is load-bearing: it FAILS against the pre-fix code (which
hashed the out-of-tree file and surfaced the hash in ChecksumMismatchError).
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from errorta_briefs.bundle_import import import_bundle
from errorta_export.import_ import import_export_bundle
from errorta_export.safe_path import UnsafePathError


def _tar_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# --- export bundle ---------------------------------------------------------

_EXPORT_REJECT_KEYS = [
    "/etc/hosts",
    "../../etc/hosts",
    "..\\..\\etc\\hosts",
    "C:\\Windows\\win.ini",
    "\\\\server\\share\\x",
    "Errorta/corpora/../files/x",
    "Errorta/corpora/ok/files/../../../../x",
]


@pytest.mark.parametrize("bad_key", _EXPORT_REJECT_KEYS)
def test_export_import_rejects_traversal_file_key(tmp_path: Path, bad_key: str) -> None:
    manifest = {
        "version": "1",
        "corpora": ["demo"],
        "files": {bad_key: {"sha256": "0" * 64, "size_bytes": 1}},
    }
    tb = tmp_path / "evil.tar.gz"
    tb.write_bytes(_tar_bytes({"export-manifest.json": json.dumps(manifest).encode()}))
    with pytest.raises(UnsafePathError):
        import_export_bundle(tb, target_home=tmp_path / "home")


def test_export_import_rejects_corpora_write_sink(tmp_path: Path) -> None:
    # The write sink: a ".." in the manifest "corpora" array drives
    # corpora_root / cname even with zero file entries.
    manifest = {"version": "1", "corpora": [".."], "files": {}}
    tb = tmp_path / "evil.tar.gz"
    tb.write_bytes(_tar_bytes({"export-manifest.json": json.dumps(manifest).encode()}))
    home = tmp_path / "home"
    with pytest.raises(UnsafePathError):
        import_export_bundle(tb, target_home=home)
    # nothing created outside the corpora root
    assert not (home).exists() or list((home).glob("../*evil*")) == []


def test_export_import_no_hash_oracle_for_out_of_tree_file(tmp_path: Path) -> None:
    """An absolute manifest key pointed at a secret must NOT leak the secret's
    sha256 in the raised error. Fails against pre-fix code."""
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"top-secret-content")
    secret_sha = hashlib.sha256(secret.read_bytes()).hexdigest()
    before = secret.stat()

    manifest = {
        "version": "1",
        "corpora": ["demo"],
        # absolute key: pre-fix `manifest_base / key` == the secret path
        "files": {str(secret): {"sha256": "1" * 64, "size_bytes": 1}},
    }
    tb = tmp_path / "evil.tar.gz"
    tb.write_bytes(_tar_bytes({"export-manifest.json": json.dumps(manifest).encode()}))

    with pytest.raises(UnsafePathError) as ei:
        import_export_bundle(tb, target_home=tmp_path / "home")

    msg = str(ei.value)
    assert secret_sha not in msg and secret_sha[:12] not in msg
    # secret untouched (not deleted/rewritten)
    assert secret.read_bytes() == b"top-secret-content"
    assert secret.stat().st_size == before.st_size


# --- brief bundle ----------------------------------------------------------


def _brief_manifest(files: list[dict], brief_id: str = "demo-brief") -> bytes:
    return json.dumps(
        {"version": 1, "brief_id": brief_id, "files": files}
    ).encode()


def test_brief_import_rejects_traversal_file_key(tmp_path: Path) -> None:
    members = {
        "bundle-manifest.json": _brief_manifest(
            [{"path": "../../etc/hosts", "sha256": "0" * 64}]
        ),
    }
    tb = tmp_path / "brief.tar.gz"
    tb.write_bytes(_tar_bytes(members))
    with pytest.raises(UnsafePathError):
        import_bundle(tb, corpus_name="default", briefs_root=tmp_path / "briefs")


def test_brief_import_rejects_traversal_brief_id(tmp_path: Path) -> None:
    members = {"bundle-manifest.json": _brief_manifest([], brief_id="..")}
    tb = tmp_path / "brief.tar.gz"
    tb.write_bytes(_tar_bytes(members))
    with pytest.raises(UnsafePathError):
        import_bundle(tb, corpus_name="default", briefs_root=tmp_path / "briefs")


def test_brief_import_rejects_traversal_rename_to(tmp_path: Path) -> None:
    members = {"bundle-manifest.json": _brief_manifest([])}
    tb = tmp_path / "brief.tar.gz"
    tb.write_bytes(_tar_bytes(members))
    with pytest.raises(UnsafePathError):
        import_bundle(
            tb, corpus_name="default", rename_to="../escape",
            briefs_root=tmp_path / "briefs",
        )


def test_brief_import_rejects_traversal_corpus_name(tmp_path: Path) -> None:
    members = {"bundle-manifest.json": _brief_manifest([])}
    tb = tmp_path / "brief.tar.gz"
    tb.write_bytes(_tar_bytes(members))
    with pytest.raises(UnsafePathError):
        import_bundle(tb, corpus_name="..", briefs_root=tmp_path / "briefs")


# --- route boundary: 400, never a 500 or a hash oracle ---------------------


def test_export_import_route_returns_400_no_oracle(tmp_errorta_home: Path) -> None:
    """The route must map a traversal bundle to a clean 400 (NOT an unhandled
    500, which carries no CORS headers and reads as 'backend unreachable'), and
    must not leak the sha256 of any out-of-tree file. The echoed `key` is the
    attacker's own manifest input, so it is not a disclosure."""
    from fastapi.testclient import TestClient
    from errorta_app.server import app

    # A secret the attacker does NOT know the hash of; a traversal key aimed
    # near it. Pre-fix code would hash it and surface the hash.
    secret = tmp_errorta_home / "secret.txt"
    secret.write_bytes(b"top-secret-content")
    secret_sha = hashlib.sha256(secret.read_bytes()).hexdigest()
    manifest = {
        "version": "1",
        "corpora": ["demo"],
        "files": {"../../../../../../etc/hosts": {"sha256": "1" * 64, "size_bytes": 1}},
    }
    tb = _tar_bytes({"export-manifest.json": json.dumps(manifest).encode()})

    client = TestClient(app)
    r = client.post(
        "/export/import",
        files={"tarball": ("evil.tar.gz", tb, "application/gzip")},
    )
    assert r.status_code == 400, r.text  # not 500
    body = r.text
    assert secret_sha not in body and secret_sha[:12] not in body
    detail = r.json().get("detail", {})
    assert isinstance(detail, dict) and detail.get("code") == "unsafe_bundle_member"
    assert secret.read_bytes() == b"top-secret-content"
