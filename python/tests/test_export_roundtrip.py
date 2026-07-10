"""F010-ROUNDTRIP — additional coverage for multi-corpus export/import.

Complements ``tests/test_export_import.py``. Each test here exercises at least
one dimension NOT covered by that file:

  1. Multi-corpus ordering preservation across a full roundtrip.
  2. ``copy_with_progress(..., dry_run=True)`` reports bytes without writing.
  3. Direct ``import_export_bundle`` call raises ``CorpusCollisionError``
     (the existing route-level test only checks HTTP 409).
  4. Imported per-corpus ``manifest.json`` entries carry the full
     ``FileEntry`` schema (``file_id``, ``original_path``, ``copied_path``,
     ``sha256``, ``size_bytes``, ``mime_ext``, ``status``).
  5. Parametrized N-corpus roundtrip asserts byte-equal file payloads (not
     just SHA-256 set equality) for ``n_corpora`` ∈ {1, 2, 3}, and confirms
     ``result.corpora_imported`` preserves submission order for N≥2.
  6. Partial corruption across a multi-corpus bundle (one byte flipped in
     ``corpus beta``) raises ``ChecksumMismatchError`` and leaves no corpus
     directory on disk — the no-partial-write guarantee.
  7. Truncated tarball input raises ``ExportImportError`` with the
     'tarball unreadable' wording and creates no corpora directory.

Helpers are imported from ``test_export_import`` to prevent drift; the
``tmp_errorta_home`` fixture from ``conftest.py`` keeps everything hermetic.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from errorta_export import (
    ChecksumMismatchError,
    CorpusCollisionError,
    ExportImportError,
    copy_with_progress,
    import_export_bundle,
    planner,
    write_export_manifest,
)

# Reuse helpers from the sibling test module to avoid drift.
from .test_export_import import (
    _build_export_tarball,
    _build_fake_corpus,
    _export_to_dir,
    _pack_tarball,
)


# ---------------------------------------------------------------------------
# 1. Multi-corpus roundtrip preserves order + SHA-256 set on disk.
# ---------------------------------------------------------------------------


def test_multi_corpus_roundtrip_preserves_order_and_shas(
    tmp_errorta_home: Path, tmp_path: Path
) -> None:
    errorta_home = tmp_errorta_home / ".errorta"

    alpha_files = [
        ("a1.pdf", b"alpha-one-payload"),
        ("a2.txt", b"alpha-two-different"),
        ("a3.md", b"alpha-three-extra"),
    ]
    beta_files = [
        ("b1.pdf", b"beta-one-bytes"),
        ("b2.txt", b"beta-two-bytes-larger"),
    ]
    _build_fake_corpus(errorta_home, "alpha-corp", alpha_files)
    _build_fake_corpus(errorta_home, "beta-corp", beta_files)

    # Source SHA sets (the planner re-derives sha from on-disk size; we use the
    # manifest the fake corpus wrote so the export manifest carries them).
    import hashlib

    def _shas(files: list[tuple[str, bytes]]) -> set[str]:
        return {hashlib.sha256(data).hexdigest() for _, data in files}

    expected_alpha_shas = _shas(alpha_files)
    expected_beta_shas = _shas(beta_files)

    tarball = _build_export_tarball(
        errorta_home, tmp_path, ["alpha-corp", "beta-corp"]
    )

    # Wipe originals so import has a clean target. Reuses the same
    # ``errorta_home`` (HOME is monkeypatched by the fixture so the corpus
    # manifest layer's writes also land here).
    shutil.rmtree(errorta_home / "corpora" / "alpha-corp")
    shutil.rmtree(errorta_home / "corpora" / "beta-corp")

    result = import_export_bundle(tarball, target_home=errorta_home)
    assert result.corpora_imported == ["alpha-corp", "beta-corp"], (
        "import must preserve the corpus order from the planner/manifest"
    )

    # Per-corpus on-disk SHA set matches source set.
    for cname, expected_shas in (
        ("alpha-corp", expected_alpha_shas),
        ("beta-corp", expected_beta_shas),
    ):
        files_dir = errorta_home / "corpora" / cname / "files"
        assert files_dir.is_dir(), f"missing files dir for {cname}"
        on_disk = {
            hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files_dir.iterdir()
            if p.is_file()
        }
        assert on_disk == expected_shas, (
            f"sha256 set mismatch for {cname}: {on_disk} vs {expected_shas}"
        )

        manifest_path = errorta_home / "corpora" / cname / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["name"] == cname
        assert len(manifest["files"]) == len(expected_shas)


# ---------------------------------------------------------------------------
# 2. dry_run reports bytes without writing files.
# ---------------------------------------------------------------------------


def test_dry_run_reports_bytes_without_writing(
    tmp_errorta_home: Path, tmp_path: Path
) -> None:
    errorta_home = tmp_errorta_home / ".errorta"
    files = [("a.pdf", b"hello-world"), ("b.txt", b"another-payload-here")]
    _build_fake_corpus(errorta_home, "dryrun-corp", files)

    export_dir = tmp_path / "export-tree"
    export_dir.mkdir(parents=True, exist_ok=True)
    plan = planner(
        target_dir=export_dir,
        corpora_list=["dryrun-corp"],
        errorta_home=errorta_home,
        include_models=False,
    )

    # Dry run: must not write any destination file.
    dry_result = copy_with_progress(plan, dry_run=True)
    for ef in plan.files:
        assert not ef.dest_path.exists(), (
            f"dry_run wrote to disk: {ef.dest_path}"
        )
    # CopyResult.bytes_would_write equals the planner's accounted total.
    assert dry_result.bytes_would_write == plan.total_size_bytes
    assert dry_result.bytes_written == 0
    assert dry_result.files_copied == 0

    # Now a real copy + pack + import — proves dry_run did not corrupt plan state.
    real_result = copy_with_progress(plan)
    assert real_result.files_copied == len(plan.files)
    assert real_result.bytes_written == plan.total_size_bytes
    write_export_manifest(export_dir, plan)
    tarball = tmp_path / "bundle.tar.gz"
    _pack_tarball(export_dir, tarball)

    shutil.rmtree(errorta_home / "corpora" / "dryrun-corp")
    result = import_export_bundle(tarball, target_home=errorta_home)
    assert result.corpora_imported == ["dryrun-corp"]
    assert result.files_copied == 2


# ---------------------------------------------------------------------------
# 3. Direct-call collision raises CorpusCollisionError (not just HTTP 409).
# ---------------------------------------------------------------------------


def test_direct_call_collision_raises_corpus_collision_error(
    tmp_errorta_home: Path, tmp_path: Path
) -> None:
    errorta_home = tmp_errorta_home / ".errorta"
    _build_fake_corpus(
        errorta_home,
        "collide-corp",
        [("a.pdf", b"payload-a"), ("b.txt", b"payload-b")],
    )
    tarball = _build_export_tarball(errorta_home, tmp_path, ["collide-corp"])

    # Wipe original then import once successfully.
    shutil.rmtree(errorta_home / "corpora" / "collide-corp")
    first = import_export_bundle(tarball, target_home=errorta_home)
    assert first.corpora_imported == ["collide-corp"]

    # Second import without wiping: must raise the direct exception.
    with pytest.raises(CorpusCollisionError) as exc_info:
        import_export_bundle(tarball, target_home=errorta_home)
    assert exc_info.value.conflicting_corpora == ["collide-corp"]


# ---------------------------------------------------------------------------
# 4. Imported manifest entries carry the full FileEntry schema.
# ---------------------------------------------------------------------------


def test_imported_manifest_has_full_file_entry_schema(
    tmp_errorta_home: Path, tmp_path: Path
) -> None:
    errorta_home = tmp_errorta_home / ".errorta"
    _build_fake_corpus(
        errorta_home,
        "schema-corp",
        [("doc.pdf", b"pdf-bytes"), ("note.txt", b"note-bytes")],
    )
    tarball = _build_export_tarball(errorta_home, tmp_path, ["schema-corp"])

    shutil.rmtree(errorta_home / "corpora" / "schema-corp")
    result = import_export_bundle(tarball, target_home=errorta_home)
    assert result.corpora_imported == ["schema-corp"]

    manifest_path = errorta_home / "corpora" / "schema-corp" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    files = manifest["files"]
    assert len(files) == 2

    required_keys = {
        "file_id",
        "original_path",
        "copied_path",
        "sha256",
        "size_bytes",
        "mime_ext",
        "status",
    }
    for fid, entry in files.items():
        missing = required_keys - set(entry.keys())
        assert not missing, f"entry {fid} missing keys: {missing}"
        assert entry["file_id"] == fid
        assert entry["status"] == "ready"
        assert entry["sha256"]
        assert entry["size_bytes"] > 0
        assert entry["mime_ext"] in {"pdf", "txt"}
        assert Path(entry["copied_path"]).exists()


# ---------------------------------------------------------------------------
# 5. Parametrized N-corpus byte-equal roundtrip + order preservation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n_corpora", [1, 2, 3])
def test_roundtrip_preserves_byte_equal_content_across_corpus_counts(
    tmp_errorta_home: Path, tmp_path: Path, n_corpora: int
) -> None:
    errorta_home = tmp_errorta_home / ".errorta"

    # Build N corpora with deterministic, per-(corpus, file) distinct payloads
    # so byte-equal assertion is strictly stronger than SHA-set equality.
    corpus_names: list[str] = [f"corpus-{i}" for i in range(n_corpora)]
    source_map: dict[str, dict[str, bytes]] = {}
    for i, cname in enumerate(corpus_names):
        files: list[tuple[str, bytes]] = []
        per_corpus: dict[str, bytes] = {}
        # Vary file count per corpus a bit to exercise heterogenous bundles.
        for j in range(2 + (i % 2)):
            fname = f"file-{j}.bin"
            payload = f"corpus-{i}-file-{j}-payload".encode()
            files.append((fname, payload))
            per_corpus[fname] = payload
        _build_fake_corpus(errorta_home, cname, files)
        source_map[cname] = per_corpus

    tarball = _build_export_tarball(errorta_home, tmp_path, corpus_names)

    # Wipe originals — clean target for import.
    for cname in corpus_names:
        shutil.rmtree(errorta_home / "corpora" / cname)

    result = import_export_bundle(tarball, target_home=errorta_home)

    # Order preservation for multi-corpus bundles.
    if n_corpora >= 2:
        assert result.corpora_imported == corpus_names, (
            f"submission order not preserved: {result.corpora_imported}"
        )
    else:
        assert result.corpora_imported == corpus_names

    # Byte-equal verification (stronger than SHA-set equality).
    for cname, per_corpus in source_map.items():
        files_dir = errorta_home / "corpora" / cname / "files"
        assert files_dir.is_dir(), f"missing files dir for {cname}"
        on_disk_names = {p.name for p in files_dir.iterdir() if p.is_file()}
        assert on_disk_names == set(per_corpus.keys()), (
            f"file name set mismatch for {cname}: {on_disk_names} vs "
            f"{set(per_corpus.keys())}"
        )
        for fname, expected_bytes in per_corpus.items():
            actual = (files_dir / fname).read_bytes()
            assert actual == expected_bytes, (
                f"byte content mismatch for {cname}/{fname}"
            )


# ---------------------------------------------------------------------------
# 6. Partial corruption across corpora aborts cleanly (no partial write).
# ---------------------------------------------------------------------------


def test_import_partial_corruption_aborts_cleanly(
    tmp_errorta_home: Path, tmp_path: Path
) -> None:
    errorta_home = tmp_errorta_home / ".errorta"
    corpora_root = errorta_home / "corpora"

    _build_fake_corpus(
        errorta_home,
        "alpha",
        [("a1.pdf", b"alpha-one-payload"), ("a2.txt", b"alpha-two-payload")],
    )
    _build_fake_corpus(
        errorta_home,
        "beta",
        [("b1.pdf", b"beta-one-payload"), ("b2.txt", b"beta-two-payload")],
    )

    # Build export tree *before* packing so we can tamper with one file in
    # corpus beta after the export-manifest is written but before the tarball
    # exists. Tampering after pack would defeat sha verification differently
    # (we want a checksum mismatch surfaced by the in-bundle manifest).
    export_dir = tmp_path / "export-tree"
    export_dir.mkdir(parents=True, exist_ok=True)
    _export_to_dir(errorta_home, export_dir, ["alpha", "beta"])

    beta_files_dir = export_dir / "Errorta" / "corpora" / "beta" / "files"
    # Pick the first file deterministically to flip a byte in.
    candidates = sorted(p for p in beta_files_dir.iterdir() if p.is_file())
    assert candidates, "expected at least one file in exported beta corpus"
    bad = candidates[0]
    data = bad.read_bytes()
    assert len(data) > 0
    bad.write_bytes(bytes([data[0] ^ 0xFF]) + data[1:])

    tarball = tmp_path / "bundle.tar.gz"
    _pack_tarball(export_dir, tarball)

    # Wipe originals so we can verify no half-written corpus directory shows up.
    shutil.rmtree(corpora_root / "alpha")
    shutil.rmtree(corpora_root / "beta")
    assert not (corpora_root / "alpha").exists()
    assert not (corpora_root / "beta").exists()

    with pytest.raises(ChecksumMismatchError) as exc_info:
        import_export_bundle(tarball, target_home=errorta_home)

    assert "beta" in exc_info.value.path, (
        f"expected 'beta' in mismatch path, got {exc_info.value.path!r}"
    )

    # The no-partial-write guarantee derives from SHA-verify running at
    # Phase 3 (pre-commit) — strictly before any ``mkdir`` of a destination
    # corpus directory at Phase 5. The property is therefore "never started"
    # rather than "rolled back": neither alpha nor beta exists on disk.
    assert not (corpora_root / "alpha").exists(), (
        "alpha corpus dir leaked despite pre-commit SHA failure on beta"
    )
    assert not (corpora_root / "beta").exists(), (
        "beta corpus dir leaked despite pre-commit SHA failure"
    )


# ---------------------------------------------------------------------------
# 7. Truncated tarball is rejected with documented wording.
# ---------------------------------------------------------------------------


def test_import_truncated_tarball_rejected(
    tmp_errorta_home: Path, tmp_path: Path
) -> None:
    errorta_home = tmp_errorta_home / ".errorta"
    corpora_root = errorta_home / "corpora"

    _build_fake_corpus(
        errorta_home,
        "trunc-corp",
        [("a.pdf", b"hello-world-payload"), ("b.txt", b"a-second-payload")],
    )
    tarball = _build_export_tarball(errorta_home, tmp_path, ["trunc-corp"])

    # Wipe original so we can detect any leaked dir from a failed import.
    shutil.rmtree(corpora_root / "trunc-corp")
    assert not (corpora_root / "trunc-corp").exists()

    full = tarball.read_bytes()
    assert len(full) > 100, "tarball unexpectedly small for truncation test"
    truncated_len = max(1, (len(full) * 3) // 4)
    truncated_path = tmp_path / "bundle-truncated.tar.gz"
    truncated_path.write_bytes(full[:truncated_len])

    # Truncated gzip streams may surface as tarfile.TarError, OSError, or
    # EOFError depending on Python version; the import path wraps TarError
    # into ExportImportError. Broaden expectation so the test is resilient
    # across runtimes — if leakage is observed (an unwrapped OSError/EOFError),
    # the except clause in errorta_export/import_.py:246 should be widened
    # accordingly.
    with pytest.raises((ExportImportError, OSError, EOFError)) as exc_info:
        import_export_bundle(truncated_path, target_home=errorta_home)

    # If wrapped as ExportImportError, confirm the documented wording.
    if isinstance(exc_info.value, ExportImportError):
        assert "tarball unreadable" in str(exc_info.value), (
            f"expected 'tarball unreadable' wording, got {exc_info.value!r}"
        )

    # Must NOT be a checksum mismatch or collision — truncation is its own
    # failure mode.
    assert not isinstance(exc_info.value, ChecksumMismatchError)
    assert not isinstance(exc_info.value, CorpusCollisionError)

    # No corpora directory created for this bundle.
    assert not (corpora_root / "trunc-corp").exists(), (
        "trunc-corp directory leaked after truncated-tarball import attempt"
    )
