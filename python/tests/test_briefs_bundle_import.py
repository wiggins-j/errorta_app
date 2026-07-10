"""BUNDLE-IMPORT — tests for the brief bundle restorer.

Hermetic: HOME is redirected via ``tmp_errorta_home`` so every read/write lands
under tmp_path. Bundles are produced in-test via :func:`build_bundle` to avoid
shipping binary fixtures.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
import textwrap
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from errorta_briefs.bundle import build_bundle
from errorta_briefs.bundle_import import (
    BriefAlreadyExists,
    BriefImportResult,
    ImportError as BundleImportError,
    MarkdownInvalid,
    import_bundle,
)


BRIEF_MD = textwrap.dedent(
    """\
    ---
    project: Bundle Import Test
    corpus: bundle-import-test
    sensitivity: Public
    refresh: manual
    sources:
      - name: fake
        config: {}
    ---

    Body prose.
    """
)


# ---------------------------------------------------------------------------
# Helpers — seed a brief, build a bundle from it, retain the tar.gz path.
# ---------------------------------------------------------------------------


def _seed_brief(home: Path, brief_id: str = "bundle-import-test") -> Path:
    brief_dir = home / ".errorta" / "corpora" / brief_id
    (brief_dir / "files").mkdir(parents=True, exist_ok=True)
    (brief_dir / "run-logs").mkdir(parents=True, exist_ok=True)

    md = BRIEF_MD.replace("bundle-import-test", brief_id)
    (brief_dir / "brief.md").write_text(md, encoding="utf-8")
    (brief_dir / "brief-manifest.json").write_text(
        json.dumps({"brief_id": brief_id, "corpus_name": brief_id}),
        encoding="utf-8",
    )
    (brief_dir / "collect-state.json").write_text(
        json.dumps({"state": "COMPLETED", "per_source": {}}), encoding="utf-8"
    )
    (brief_dir / "dedup-index.json").write_text("{}", encoding="utf-8")
    (brief_dir / "run-extras.json").write_text(
        json.dumps({"per_source": {}}), encoding="utf-8"
    )
    (brief_dir / "run-logs" / "run-001.log").write_text("hello\n", encoding="utf-8")

    f1 = brief_dir / "files" / "doc1.pdf"
    f2 = brief_dir / "files" / "doc2.pdf"
    f1.write_bytes(b"PDF-A" * 1024)
    f2.write_bytes(b"PDF-B" * 2048)
    cmanifest = {
        "name": brief_id,
        "files": {
            "fid_1": {
                "file_id": "fid_1",
                "original_path": "/orig/doc1.pdf",
                "copied_path": str(f1),
                "sha256": "deadbeef",
                "size_bytes": f1.stat().st_size,
                "mime_ext": "pdf",
                "status": "ready",
                "error": None,
                "chunk_count": 0,
                "chunk_ids": [],
                "token_count": 0,
                "ingested_at": None,
                "progress": 1.0,
            },
            "fid_2": {
                "file_id": "fid_2",
                "original_path": "/orig/doc2.pdf",
                "copied_path": str(f2),
                "sha256": "feedface",
                "size_bytes": f2.stat().st_size,
                "mime_ext": "pdf",
                "status": "ready",
                "error": None,
                "chunk_count": 0,
                "chunk_ids": [],
                "token_count": 0,
                "ingested_at": None,
                "progress": 1.0,
            },
        },
    }
    (brief_dir / "manifest.json").write_text(
        json.dumps(cmanifest, indent=2), encoding="utf-8"
    )
    return brief_dir


def _build_bundle_for(home: Path, brief_id: str = "bundle-import-test") -> Path:
    _seed_brief(home, brief_id)
    out = home / "out"
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"{brief_id}.tar.gz"
    build_bundle(brief_id, dest, dry_run=False)
    assert dest.exists()
    return dest


def _wipe_brief_dir(home: Path, brief_id: str = "bundle-import-test") -> None:
    """Remove the seeded brief dir so import has a clean target."""
    p = home / ".errorta" / "corpora" / brief_id
    if p.exists():
        shutil.rmtree(p)


# ---------------------------------------------------------------------------
# (a) Round-trip export → import → contents equal.
# ---------------------------------------------------------------------------


def test_round_trip_export_import(tmp_errorta_home: Path) -> None:
    tar = _build_bundle_for(tmp_errorta_home)
    _wipe_brief_dir(tmp_errorta_home)

    result = import_bundle(tar, corpus_name="restored")
    assert isinstance(result, BriefImportResult)
    assert result.brief_id == "bundle-import-test"
    assert result.corpus_name == "restored"
    assert result.files_imported > 0

    restored = tmp_errorta_home / ".errorta" / "corpora" / "bundle-import-test"
    assert (restored / "brief.md").exists()
    manifest_path = restored / "brief-manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["state"] == "DRAFT"
    assert manifest["last_run_at"] is None
    assert manifest["runs"] == []
    assert manifest["brief_id"] == "bundle-import-test"
    assert manifest["corpus_name"] == "restored"

    # Corpus payload mirrored.
    assert (restored / "files" / "doc1.pdf").exists()
    assert (restored / "files" / "doc2.pdf").exists()


# ---------------------------------------------------------------------------
# (b) Corrupt one bundled file → 400 with which file failed.
# ---------------------------------------------------------------------------


def _rewrite_bundle_with_corruption(tar_path: Path, target_basename: str) -> Path:
    """Repack ``tar_path`` flipping a byte in any file ending with ``target_basename``."""
    out = tar_path.with_suffix(".corrupt.tar.gz")
    with tarfile.open(tar_path, "r:gz") as src, tarfile.open(out, "w:gz") as dst:
        for member in src.getmembers():
            f = src.extractfile(member) if member.isfile() else None
            if f is not None and member.name.endswith(target_basename):
                data = f.read() + b"\x00CORRUPT"
                info = tarfile.TarInfo(name=member.name)
                info.size = len(data)
                info.mode = member.mode
                info.mtime = member.mtime
                dst.addfile(info, io.BytesIO(data))
            elif f is not None:
                data = f.read()
                info = tarfile.TarInfo(name=member.name)
                info.size = len(data)
                info.mode = member.mode
                info.mtime = member.mtime
                dst.addfile(info, io.BytesIO(data))
            else:
                dst.addfile(member)
    return out


def test_corrupt_file_in_bundle_raises_with_path(tmp_errorta_home: Path) -> None:
    tar = _build_bundle_for(tmp_errorta_home)
    _wipe_brief_dir(tmp_errorta_home)
    corrupt = _rewrite_bundle_with_corruption(tar, "doc1.pdf")
    with pytest.raises(BundleImportError) as exc_info:
        import_bundle(corrupt, corpus_name="restored")
    assert "doc1.pdf" in str(exc_info.value)
    # Nothing landed on disk.
    assert not (tmp_errorta_home / ".errorta" / "corpora" / "bundle-import-test").exists()


# ---------------------------------------------------------------------------
# (c) Double-import same bundle → BriefAlreadyExists / 409.
# ---------------------------------------------------------------------------


def test_double_import_raises_already_exists(tmp_errorta_home: Path) -> None:
    tar = _build_bundle_for(tmp_errorta_home)
    _wipe_brief_dir(tmp_errorta_home)
    import_bundle(tar, corpus_name="restored")
    with pytest.raises(BriefAlreadyExists) as exc_info:
        import_bundle(tar, corpus_name="restored")
    assert exc_info.value.brief_id == "bundle-import-test"
    assert exc_info.value.corpus_name == "restored"


# ---------------------------------------------------------------------------
# (d) Path-traversal member → rejected, nothing written.
# ---------------------------------------------------------------------------


def _make_traversal_bundle(dest: Path) -> Path:
    with tarfile.open(dest, "w:gz") as tf:
        data = b"escaped"
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return dest


def test_traversal_member_rejected(tmp_errorta_home: Path) -> None:
    tar = tmp_errorta_home / "evil.tar.gz"
    _make_traversal_bundle(tar)
    with pytest.raises(BundleImportError):
        import_bundle(tar, corpus_name="restored")
    # No escape on disk.
    assert not (tmp_errorta_home.parent / "escape.txt").exists()
    assert not (tmp_errorta_home / "escape.txt").exists()


# ---------------------------------------------------------------------------
# (e) Symlink member → rejected.
# ---------------------------------------------------------------------------


def test_symlink_member_rejected(tmp_errorta_home: Path) -> None:
    tar = tmp_errorta_home / "evil-link.tar.gz"
    with tarfile.open(tar, "w:gz") as tf:
        info = tarfile.TarInfo(name="link.txt")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    with pytest.raises(BundleImportError):
        import_bundle(tar, corpus_name="restored")


# ---------------------------------------------------------------------------
# (f) Corrupted brief.md (parser failure) → MarkdownInvalid / 422.
# ---------------------------------------------------------------------------


def _build_bundle_with_bad_brief_md(home: Path) -> Path:
    """Build a normal bundle, then repack swapping brief.md with garbage that
    re-passes the bundle's per-file sha (we update the manifest entry too)."""
    tar = _build_bundle_for(home)
    out = tar.with_suffix(".badmd.tar.gz")
    # Extract → tweak brief.md + bundle-manifest.json → repack.
    extract = home / "extract-bad"
    extract.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar, "r:gz") as tf:
        tf.extractall(extract)
    roots = [p for p in extract.iterdir() if p.is_dir()]
    root = roots[0]
    bad_md = "no frontmatter here\nplain text\n"
    (root / "brief.md").write_text(bad_md, encoding="utf-8")
    new_sha = hashlib.sha256(bad_md.encode("utf-8")).hexdigest()
    manifest = json.loads((root / "bundle-manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] == "brief.md":
            entry["sha256"] = new_sha
            entry["size_bytes"] = len(bad_md.encode("utf-8"))
    (root / "bundle-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    with tarfile.open(out, "w:gz") as tf:
        tf.add(str(root), arcname=root.name)
    return out


def test_corrupted_brief_md_raises_markdown_invalid(tmp_errorta_home: Path) -> None:
    tar = _build_bundle_with_bad_brief_md(tmp_errorta_home)
    _wipe_brief_dir(tmp_errorta_home)
    with pytest.raises(MarkdownInvalid):
        import_bundle(tar, corpus_name="restored")
    # Partial dir cleaned.
    assert not (tmp_errorta_home / ".errorta" / "corpora" / "bundle-import-test").exists()


# ---------------------------------------------------------------------------
# (g) Corpus manifest with copied_path rewritten + save_manifest called.
# ---------------------------------------------------------------------------


def test_corpus_manifest_copied_path_rewritten(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tar = _build_bundle_for(tmp_errorta_home)
    _wipe_brief_dir(tmp_errorta_home)

    from errorta_corpus import manifest as cm

    captured: dict[str, dict] = {}

    real_save = cm.save_manifest

    def spy_save(name: str, files):  # type: ignore[no-untyped-def]
        captured["name"] = name
        captured["files"] = files
        return real_save(name, files)

    monkeypatch.setattr(cm, "save_manifest", spy_save)

    import_bundle(tar, corpus_name="restored-corpus")
    assert captured["name"] == "restored-corpus"
    files = captured["files"]
    assert set(files.keys()) == {"fid_1", "fid_2"}
    restored_root = (
        tmp_errorta_home / ".errorta" / "corpora" / "bundle-import-test" / "files"
    )
    for fid, entry in files.items():
        assert entry.copied_path.startswith(str(restored_root))


# ---------------------------------------------------------------------------
# (h) Failure mid-extract → no partial brief dir on disk.
# ---------------------------------------------------------------------------


def test_failure_mid_extract_leaves_no_partial_dir(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tar = _build_bundle_for(tmp_errorta_home)
    _wipe_brief_dir(tmp_errorta_home)

    import errorta_briefs.bundle_import as bi

    real_copy = bi.shutil.copy2
    state = {"calls": 0}

    def boom_after_first(src, dst, *a, **kw):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] > 1:
            raise RuntimeError("synthetic mid-copy failure")
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(bi.shutil, "copy2", boom_after_first)
    with pytest.raises(RuntimeError, match="synthetic mid-copy failure"):
        import_bundle(tar, corpus_name="restored")

    # Target dir must be gone.
    assert not (tmp_errorta_home / ".errorta" / "corpora" / "bundle-import-test").exists()


# ---------------------------------------------------------------------------
# (i) rename_to respected end-to-end.
# ---------------------------------------------------------------------------


def test_rename_to_respected(tmp_errorta_home: Path) -> None:
    tar = _build_bundle_for(tmp_errorta_home)
    _wipe_brief_dir(tmp_errorta_home)
    import_bundle(tar, corpus_name="restored")
    # Now re-import same bundle under a new id.
    result = import_bundle(tar, corpus_name="restored", rename_to="renamed-brief")
    assert result.brief_id == "renamed-brief"
    target = tmp_errorta_home / ".errorta" / "corpora" / "renamed-brief"
    assert (target / "brief.md").exists()
    manifest = json.loads((target / "brief-manifest.json").read_text(encoding="utf-8"))
    assert manifest["brief_id"] == "renamed-brief"


# ---------------------------------------------------------------------------
# Route tests — happy path + 409 + 400.
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_errorta_home: Path) -> Iterator[TestClient]:
    from errorta_app.server import app

    with TestClient(app) as c:
        yield c


def test_route_happy_path(client: TestClient, tmp_errorta_home: Path) -> None:
    tar = _build_bundle_for(tmp_errorta_home)
    _wipe_brief_dir(tmp_errorta_home)
    with open(tar, "rb") as fh:
        r = client.post(
            "/briefs/import-bundle?corpus_name=restored",
            files={"tarball": ("bundle.tar.gz", fh, "application/gzip")},
        )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["brief_id"] == "bundle-import-test"
    assert payload["corpus_name"] == "restored"
    assert payload["files_imported"] > 0
    assert isinstance(payload["warnings"], list)
    assert payload["timestamp_imported"]


def test_route_409_on_collision(client: TestClient, tmp_errorta_home: Path) -> None:
    tar = _build_bundle_for(tmp_errorta_home)
    _wipe_brief_dir(tmp_errorta_home)
    with open(tar, "rb") as fh:
        r1 = client.post(
            "/briefs/import-bundle?corpus_name=restored",
            files={"tarball": ("bundle.tar.gz", fh, "application/gzip")},
        )
    assert r1.status_code == 200, r1.text

    with open(tar, "rb") as fh:
        r2 = client.post(
            "/briefs/import-bundle?corpus_name=restored",
            files={"tarball": ("bundle.tar.gz", fh, "application/gzip")},
        )
    assert r2.status_code == 409
    detail = r2.json()["detail"]
    assert detail["code"] == "already_exists"
    assert "rename_to" in detail["message"]

    # Retry with rename_to.
    with open(tar, "rb") as fh:
        r3 = client.post(
            "/briefs/import-bundle?corpus_name=restored&rename_to=renamed-via-route",
            files={"tarball": ("bundle.tar.gz", fh, "application/gzip")},
        )
    assert r3.status_code == 200, r3.text
    assert r3.json()["brief_id"] == "renamed-via-route"


def test_route_400_on_traversal(client: TestClient, tmp_errorta_home: Path) -> None:
    tar = tmp_errorta_home / "evil.tar.gz"
    _make_traversal_bundle(tar)
    with open(tar, "rb") as fh:
        r = client.post(
            "/briefs/import-bundle?corpus_name=restored",
            files={"tarball": ("evil.tar.gz", fh, "application/gzip")},
        )
    assert r.status_code == 400
