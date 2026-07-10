"""F010-IMPORT — round-trip tests for /export/import + import_export_bundle.

Hermetic: redirects HOME to tmp via ``tmp_errorta_home``, builds a fake corpus,
exports it via the real export module (planner + copy + manifest), packs the
result into a tarball, and feeds that tarball into the import endpoint or the
``import_export_bundle`` function.
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from errorta_export import (
    ChecksumMismatchError,
    CorpusCollisionError,
    ImportResult,
    ManifestMissingError,
    UnsafeMemberError,
    copy_with_progress,
    import_export_bundle,
    planner,
    write_export_manifest,
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_fake_corpus(
    errorta_home: Path,
    corpus_name: str,
    files: list[tuple[str, bytes]],
) -> None:
    corpus_dir = errorta_home / "corpora" / corpus_name
    files_dir = corpus_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict] = {}
    for i, (name, content) in enumerate(files):
        p = files_dir / name
        p.write_bytes(content)
        fid = f"f{i:03d}"
        entries[fid] = {
            "file_id": fid,
            "original_path": str(p),
            "copied_path": str(p),
            "sha256": _sha256_bytes(content),
            "size_bytes": len(content),
            "mime_ext": name.split(".")[-1],
            "status": "ready",
        }
    (corpus_dir / "manifest.json").write_text(
        json.dumps({"name": corpus_name, "files": entries})
    )


def _export_to_dir(errorta_home: Path, target_dir: Path, corpora: list[str]) -> Path:
    """Use the real export module to produce a Errorta/corpora/... tree in target_dir.

    Returns target_dir.
    """
    plan = planner(
        target_dir=target_dir,
        corpora_list=corpora,
        errorta_home=errorta_home,
        include_models=False,
    )
    copy_with_progress(plan)
    write_export_manifest(target_dir, plan)
    return target_dir


def _pack_tarball(src_dir: Path, dest_tarball: Path, *, gz: bool = True) -> Path:
    """Pack src_dir into a tar.gz at dest_tarball; archive layout has no top-level dir."""
    mode = "w:gz" if gz else "w"
    with tarfile.open(dest_tarball, mode=mode) as tf:
        for entry in sorted(src_dir.rglob("*")):
            if entry.is_file():
                tf.add(entry, arcname=str(entry.relative_to(src_dir)))
    return dest_tarball


def _build_export_tarball(
    errorta_home: Path, tmp_path: Path, corpora: list[str]
) -> Path:
    """Round-trip helper: corpus → export dir → tarball."""
    export_dir = tmp_path / "export-tree"
    export_dir.mkdir(parents=True, exist_ok=True)
    _export_to_dir(errorta_home, export_dir, corpora)
    tarball = tmp_path / "bundle.tar.gz"
    _pack_tarball(export_dir, tarball)
    return tarball


@pytest.fixture
def client(tmp_errorta_home: Path) -> Iterator[TestClient]:
    from errorta_app.server import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Happy path — POST /export/import creates corpus + manifest on disk.
# ---------------------------------------------------------------------------


def test_import_happy_path_creates_corpus_and_manifest(
    client: TestClient, tmp_errorta_home: Path, tmp_path: Path
) -> None:
    errorta_home = tmp_errorta_home / ".errorta"
    _build_fake_corpus(
        errorta_home,
        "alpha",
        [("a.pdf", b"hello"), ("b.txt", b"world!")],
    )
    tarball = _build_export_tarball(errorta_home, tmp_path, ["alpha"])

    # Wipe original so the import has a clean target.
    import shutil

    shutil.rmtree(errorta_home / "corpora" / "alpha")
    assert not (errorta_home / "corpora" / "alpha").exists()

    with open(tarball, "rb") as f:
        r = client.post(
            "/export/import",
            files={"tarball": ("bundle.tar.gz", f, "application/gzip")},
        )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["corpora_imported"] == ["alpha"]
    assert payload["files_copied"] == 2
    assert (errorta_home / "corpora" / "alpha" / "files" / "a.pdf").exists()
    assert (errorta_home / "corpora" / "alpha" / "files" / "b.txt").exists()
    manifest = json.loads(
        (errorta_home / "corpora" / "alpha" / "manifest.json").read_text()
    )
    assert manifest["name"] == "alpha"
    assert len(manifest["files"]) == 2


# ---------------------------------------------------------------------------
# 2. Collision returns 409 + conflicting_corpora list.
# ---------------------------------------------------------------------------


def test_import_to_existing_corpus_returns_409(
    client: TestClient, tmp_errorta_home: Path, tmp_path: Path
) -> None:
    errorta_home = tmp_errorta_home / ".errorta"
    _build_fake_corpus(errorta_home, "alpha", [("a.pdf", b"hello")])
    tarball = _build_export_tarball(errorta_home, tmp_path, ["alpha"])
    # Leave the original alpha in place — import should collide.

    with open(tarball, "rb") as f:
        r = client.post(
            "/export/import",
            files={"tarball": ("bundle.tar.gz", f, "application/gzip")},
        )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["conflicting_corpora"] == ["alpha"]


# ---------------------------------------------------------------------------
# 3. SHA mismatch — tamper a file inside the tarball, expect 422.
# ---------------------------------------------------------------------------


def test_import_with_sha_mismatch_rejects(
    tmp_errorta_home: Path, tmp_path: Path
) -> None:
    errorta_home = tmp_errorta_home / ".errorta"
    _build_fake_corpus(errorta_home, "alpha", [("a.pdf", b"hello")])
    # Build the export dir then tamper one file before packing.
    export_dir = tmp_path / "export-tree"
    export_dir.mkdir(parents=True, exist_ok=True)
    _export_to_dir(errorta_home, export_dir, ["alpha"])
    bad = export_dir / "Errorta" / "corpora" / "alpha" / "files" / "a.pdf"
    bad.write_bytes(b"TAMPERED")
    tarball = tmp_path / "bundle.tar.gz"
    _pack_tarball(export_dir, tarball)

    # Wipe original so collision isn't the failure mode.
    import shutil

    shutil.rmtree(errorta_home / "corpora" / "alpha")

    with pytest.raises(ChecksumMismatchError):
        import_export_bundle(tarball, target_home=errorta_home)
    # No partial corpus dir left behind.
    assert not (errorta_home / "corpora" / "alpha").exists()


# ---------------------------------------------------------------------------
# 4. Symlink member rejected.
# ---------------------------------------------------------------------------


def test_import_symlink_member_rejected(
    tmp_errorta_home: Path, tmp_path: Path
) -> None:
    errorta_home = tmp_errorta_home / ".errorta"
    # Hand-craft a tarball with a symlink member.
    tarball = tmp_path / "malicious.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        # Add a valid manifest so we'd otherwise pass to phase 2.
        manifest_bytes = json.dumps({"version": "1", "files": {}, "corpora": ["x"]}).encode()
        info = tarfile.TarInfo(name="export-manifest.json")
        info.size = len(manifest_bytes)
        tf.addfile(info, io.BytesIO(manifest_bytes))
        # Now add a symlink member.
        sym = tarfile.TarInfo(name="evil-link")
        sym.type = tarfile.SYMTYPE
        sym.linkname = "/etc/passwd"
        tf.addfile(sym)

    with pytest.raises(UnsafeMemberError):
        import_export_bundle(tarball, target_home=errorta_home)


# ---------------------------------------------------------------------------
# 5. Imported corpus manifest is well-formed.
# ---------------------------------------------------------------------------


def test_import_recreates_corpus_manifest(
    tmp_errorta_home: Path, tmp_path: Path
) -> None:
    errorta_home = tmp_errorta_home / ".errorta"
    _build_fake_corpus(
        errorta_home,
        "beta",
        [("a.pdf", b"AAAA"), ("b.txt", b"BB")],
    )
    tarball = _build_export_tarball(errorta_home, tmp_path, ["beta"])

    import shutil

    shutil.rmtree(errorta_home / "corpora" / "beta")
    result = import_export_bundle(tarball, target_home=errorta_home)
    assert isinstance(result, ImportResult)
    assert result.corpora_imported == ["beta"]
    assert result.files_copied == 2

    manifest_path = errorta_home / "corpora" / "beta" / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["name"] == "beta"
    files = manifest["files"]
    assert len(files) == 2
    # Every FileEntry has the expected required fields.
    for fid, entry in files.items():
        assert entry["file_id"] == fid
        assert entry["status"] == "ready"
        assert entry["sha256"]
        assert entry["size_bytes"] >= 0
        assert Path(entry["copied_path"]).exists()


# ---------------------------------------------------------------------------
# 6. Missing manifest returns 400.
# ---------------------------------------------------------------------------


def test_import_missing_manifest_returns_400(
    client: TestClient, tmp_errorta_home: Path, tmp_path: Path
) -> None:
    # Tarball with a single unrelated file, no export-manifest.json.
    tarball = tmp_path / "nomanifest.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        payload = b"unrelated"
        info = tarfile.TarInfo(name="some-other-file.txt")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))

    with open(tarball, "rb") as f:
        r = client.post(
            "/export/import",
            files={"tarball": ("nomanifest.tar.gz", f, "application/gzip")},
        )
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# 7. Partial failure cleans up created corpus dirs.
# ---------------------------------------------------------------------------


def test_import_partial_failure_cleans_up(
    tmp_errorta_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    errorta_home = tmp_errorta_home / ".errorta"
    _build_fake_corpus(errorta_home, "gamma", [("a.pdf", b"hello")])
    _build_fake_corpus(errorta_home, "delta", [("b.txt", b"world")])
    tarball = _build_export_tarball(errorta_home, tmp_path, ["gamma", "delta"])

    # Wipe originals so import would normally succeed.
    import shutil

    shutil.rmtree(errorta_home / "corpora" / "gamma")
    shutil.rmtree(errorta_home / "corpora" / "delta")

    # Force save_manifest to blow up after the first corpus dir is created.
    import errorta_export.import_ as imp_mod

    real_save = imp_mod.save_manifest  # type: ignore[attr-defined]
    call_count = {"n": 0}

    def bomb(name: str, files):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        if call_count["n"] >= 1:
            raise RuntimeError("simulated mid-import explosion")
        real_save(name, files)

    monkeypatch.setattr(imp_mod, "save_manifest", bomb)

    with pytest.raises(RuntimeError):
        import_export_bundle(tarball, target_home=errorta_home)

    # No half-populated corpora left on disk.
    assert not (errorta_home / "corpora" / "gamma").exists()
    assert not (errorta_home / "corpora" / "delta").exists()
