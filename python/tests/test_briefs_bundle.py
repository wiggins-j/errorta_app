"""F008-BUNDLE — unit + route tests for the brief bundle exporter.

Hermetic: HOME is redirected via ``tmp_errorta_home`` so every read/write lands
under tmp_path. No network. The tests cover:

  * ``build_bundle(dry_run=True)`` walks files, returns counts + per-file shas,
    writes nothing.
  * ``build_bundle(dry_run=False)`` produces a verifiable tar.gz whose
    extracted contents match the bundle-manifest.json shas.
  * Missing brief raises ``BundleError`` (and the route returns 404 with no
    partial files left behind).
  * Atomic cleanup — the sibling temp file vanishes on failure.
  * SSE route happy-path emits hello -> phase:planning -> file* -> phase:packaging
    -> phase:verifying -> done.
"""
from __future__ import annotations

import json
import tarfile
import textwrap
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from errorta_briefs.bundle import BundleError, build_bundle


BRIEF_MD = textwrap.dedent(
    """\
    ---
    project: Bundle Test
    corpus: bundle-test
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
# Helpers
# ---------------------------------------------------------------------------


def _seed_brief(home: Path, *, with_corpus: bool = True) -> Path:
    """Populate ~/.errorta/corpora/bundle-test with a realistic brief tree.

    Returns the brief directory.
    """
    brief_dir = home / ".errorta" / "corpora" / "bundle-test"
    (brief_dir / "files").mkdir(parents=True, exist_ok=True)
    (brief_dir / "run-logs").mkdir(parents=True, exist_ok=True)

    (brief_dir / "brief.md").write_text(BRIEF_MD, encoding="utf-8")
    (brief_dir / "brief-manifest.json").write_text(
        json.dumps({"brief_id": "bundle-test", "corpus_name": "bundle-test"}),
        encoding="utf-8",
    )
    (brief_dir / "collect-state.json").write_text(
        json.dumps({"state": "COMPLETED", "per_source": {}}), encoding="utf-8"
    )
    (brief_dir / "dedup-index.json").write_text("{}", encoding="utf-8")
    (brief_dir / "run-extras.json").write_text(
        json.dumps({"per_source": {}}), encoding="utf-8"
    )
    (brief_dir / "run-logs" / "run-001.log").write_text("hello log\n", encoding="utf-8")

    if with_corpus:
        f1 = brief_dir / "files" / "doc1.pdf"
        f2 = brief_dir / "files" / "doc2.pdf"
        f1.write_bytes(b"PDF-A" * 1024)
        f2.write_bytes(b"PDF-B" * 2048)
        manifest = {
            "name": "bundle-test",
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
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
    return brief_dir


# ---------------------------------------------------------------------------
# build_bundle unit tests
# ---------------------------------------------------------------------------


def test_dry_run_returns_counts_and_writes_nothing(tmp_errorta_home: Path) -> None:
    _seed_brief(tmp_errorta_home)
    dest = tmp_errorta_home / "out" / "bundle.tar.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)

    result = build_bundle("bundle-test", dest, dry_run=True)

    assert result.dry_run is True
    assert result.sha256_hex == ""
    # brief.md + brief-manifest.json + collect-state.json + dedup-index.json
    # + run-extras.json + run-logs/run-001.log + corpus-manifest.json
    # + 2 corpus files = 9 entries.
    assert result.file_count == 9
    assert len(result.files) == 9
    assert result.total_size_bytes > 0
    # The destination must NOT have been created.
    assert not dest.exists()
    # Every record has a non-empty sha and a sensible relative path.
    paths = {r.path for r in result.files}
    assert "brief.md" in paths
    assert "corpus-manifest.json" in paths
    assert any(p.startswith("corpus/files/") for p in paths)
    for r in result.files:
        assert len(r.sha256) == 64


def test_real_run_produces_verifiable_tar_gz(tmp_errorta_home: Path) -> None:
    _seed_brief(tmp_errorta_home)
    dest = tmp_errorta_home / "out" / "bundle.tar.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)

    result = build_bundle("bundle-test", dest, dry_run=False)

    assert dest.exists()
    assert result.sha256_hex and len(result.sha256_hex) == 64

    # Extract and walk.
    extract = tmp_errorta_home / "extracted"
    extract.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "r:gz") as tf:
        tf.extractall(extract)

    # There is exactly one top-level directory named brief-bundle-test-*.
    roots = [p for p in extract.iterdir() if p.is_dir()]
    assert len(roots) == 1
    root = roots[0]
    assert root.name.startswith("brief-bundle-test-")

    # Required files are present.
    assert (root / "brief.md").exists()
    assert (root / "bundle-manifest.json").exists()
    assert (root / "corpus-manifest.json").exists()
    assert (root / "corpus" / "files").is_dir()
    corpus_files = list((root / "corpus" / "files").iterdir())
    assert len(corpus_files) == 2

    # Per-file sha verification: every entry in bundle-manifest.json matches
    # what's actually on disk after extraction.
    import hashlib

    manifest = json.loads((root / "bundle-manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert manifest["brief_id"] == "bundle-test"
    assert manifest["file_count"] == len(manifest["files"])
    for entry in manifest["files"]:
        on_disk = root / entry["path"]
        assert on_disk.exists(), entry["path"]
        h = hashlib.sha256()
        h.update(on_disk.read_bytes())
        assert h.hexdigest() == entry["sha256"], entry["path"]
        assert on_disk.stat().st_size == entry["size_bytes"]


def test_missing_brief_raises_bundle_error(tmp_errorta_home: Path) -> None:
    dest = tmp_errorta_home / "out" / "nope.tar.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(BundleError):
        build_bundle("does-not-exist", dest, dry_run=False)
    assert not dest.exists()


def test_atomic_cleanup_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_errorta_home: Path) -> None:
    """If tar.gz writing blows up, the sibling temp file is removed."""
    _seed_brief(tmp_errorta_home)
    dest = tmp_errorta_home / "out" / "bundle.tar.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)

    import errorta_briefs.bundle as bundle_mod

    original_open = bundle_mod.tarfile.open

    def boom(*a, **kw):  # type: ignore[no-untyped-def]
        # Create what looks like a partial file via the real opener first
        # so the tmp_archive Path exists, then raise.
        tf = original_open(*a, **kw)
        tf.close()
        raise RuntimeError("synthetic tar failure")

    monkeypatch.setattr(bundle_mod.tarfile, "open", boom)

    with pytest.raises(RuntimeError, match="synthetic tar failure"):
        build_bundle("bundle-test", dest, dry_run=False)

    # No partial tar.gz at dest.
    assert not dest.exists()
    # No sibling .errorta-bundle-* tmp files lingering.
    leftovers = list(dest.parent.glob(".errorta-bundle-*"))
    assert leftovers == [], f"leftover tmp files: {leftovers}"


# ---------------------------------------------------------------------------
# SSE route tests
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_errorta_home: Path) -> Iterator[TestClient]:
    from errorta_app.server import app

    with TestClient(app) as c:
        yield c


def _parse_sse(body_bytes: bytes) -> list[tuple[str, dict]]:
    """Parse an SSE response body into a list of (event, payload) tuples.

    Comment frames (``: keepalive``) are skipped. Hello frames carry an empty
    payload object.
    """
    out: list[tuple[str, dict]] = []
    text = body_bytes.decode("utf-8")
    for frame in text.split("\n\n"):
        frame = frame.strip()
        if not frame or frame.startswith(":"):
            continue
        event = "message"
        data = "{}"
        for line in frame.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        try:
            payload = json.loads(data) if data else {}
        except json.JSONDecodeError:
            payload = {"_raw": data}
        out.append((event, payload))
    return out


def test_export_bundle_sse_happy_path(
    client: TestClient, tmp_errorta_home: Path
) -> None:
    _seed_brief(tmp_errorta_home)
    out_dir = tmp_errorta_home / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Create the brief via the API so the route's own state model is honored.
    # (Seeding already put brief.md + brief-manifest.json on disk under the
    # correct slug, so the route's lookup helpers see the brief immediately.)

    r = client.post(
        f"/briefs/bundle-test/export-bundle",
        json={"target_dir": str(out_dir), "dry_run": False},
    )
    assert r.status_code == 200, r.text
    events = _parse_sse(r.content)
    names = [e for e, _ in events]
    assert names[0] == "hello"
    assert "phase" in names
    phases = [p.get("phase") for n, p in events if n == "phase"]
    assert "planning" in phases
    assert "packaging" in phases
    assert "verifying" in phases
    # At least one file event.
    assert any(n == "file" for n, _ in events)
    # Terminal done event with dest_path.
    done_events = [p for n, p in events if n == "done"]
    assert done_events, events
    done = done_events[-1]
    assert done["file_count"] > 0
    assert done["sha256_hex"]
    # The archive is actually on disk.
    assert Path(done["dest_path"]).exists()


def test_export_bundle_unknown_brief_returns_404(
    client: TestClient, tmp_errorta_home: Path
) -> None:
    out_dir = tmp_errorta_home / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    r = client.post(
        f"/briefs/no-such-brief/export-bundle",
        json={"target_dir": str(out_dir), "dry_run": False},
    )
    assert r.status_code == 404
    # No partial files left behind.
    assert list(out_dir.iterdir()) == []
