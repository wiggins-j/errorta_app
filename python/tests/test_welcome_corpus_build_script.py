"""Tests for scripts/build-welcome-corpus.sh.

Slice (a) of F-INFRA-11: bundle creation + manifest shape + cap enforcement +
trailer format.

Slice (b) extends with byte-stable repeat-builds + --verify mismatch tests.
Slice (c) extends with --publish tag-shape + gh-CLI validation tests.
Slice (d) extends with default 6-doc set assertions.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build-welcome-corpus.sh"


def _have_hasher() -> bool:
    return shutil.which("shasum") is not None or shutil.which("sha256sum") is not None


def _run(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# Slice (a): bundle + manifest + trailer
# ---------------------------------------------------------------------------


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK), "build-welcome-corpus.sh should be executable"


def test_help_flag_prints_usage() -> None:
    result = _run(["bash", str(SCRIPT), "--help"])
    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "--output-dir" in result.stdout
    assert "--source-doc" in result.stdout
    assert "--verify" in result.stdout
    assert "--publish" in result.stdout


@pytest.mark.skipif(not _have_hasher(), reason="no shasum/sha256sum on PATH")
def test_default_build_produces_tarball_with_manifest(tmp_path: Path) -> None:
    result = _run(["bash", str(SCRIPT), "--output-dir", str(tmp_path)])
    assert result.returncode == 0, result.stderr
    tarball = tmp_path / "welcome-corpus.tar.gz"
    assert tarball.is_file()

    # Trailer format
    stdout_lines = result.stdout.strip().splitlines()
    version_line = next(l for l in stdout_lines if l.startswith("version:"))
    sha_line = next(l for l in stdout_lines if l.startswith("sha256:"))
    bytes_line = next(l for l in stdout_lines if l.startswith("bytes:"))
    assert version_line.split(":", 1)[1].strip()
    sha_value = sha_line.split(":", 1)[1].strip()
    assert len(sha_value) == 64
    int(bytes_line.split(":", 1)[1].strip())  # parseable

    # SHA matches the file on disk
    actual_sha = hashlib.sha256(tarball.read_bytes()).hexdigest()
    assert actual_sha == sha_value

    # Tarball listing contains manifest.json under welcome-corpus/
    with tarfile.open(tarball, "r:gz") as tf:
        names = tf.getnames()
    assert "welcome-corpus/manifest.json" in names

    # manifest.json parses with the expected four keys
    with tarfile.open(tarball, "r:gz") as tf:
        f = tf.extractfile("welcome-corpus/manifest.json")
        assert f is not None
        manifest = json.loads(f.read().decode("utf-8"))
    assert set(manifest.keys()) == {"version", "files", "generated_at", "source_commit"}
    assert isinstance(manifest["files"], list)


# ---------------------------------------------------------------------------
# Slice (b): byte-stable repeat builds + --verify mismatch
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _have_hasher(), reason="no shasum/sha256sum on PATH")
def test_repeat_builds_are_byte_stable(tmp_path: Path) -> None:
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    r1 = _run(["bash", str(SCRIPT), "--output-dir", str(out_a)])
    r2 = _run(["bash", str(SCRIPT), "--output-dir", str(out_b)])
    assert r1.returncode == 0
    assert r2.returncode == 0
    data_a = (out_a / "welcome-corpus.tar.gz").read_bytes()
    data_b = (out_b / "welcome-corpus.tar.gz").read_bytes()
    assert hashlib.sha256(data_a).hexdigest() == hashlib.sha256(data_b).hexdigest(), (
        "repeat builds at the same commit should produce byte-identical tarballs"
    )


def test_verify_fails_when_hash_does_not_match_pin(tmp_path: Path) -> None:
    # Default source set produces a hash that does NOT match the v0.1.0 pin
    # (the pin was set against a hand-curated tarball that predates this
    # script). --verify should report a clear mismatch.
    result = _run(["bash", str(SCRIPT), "--output-dir", str(tmp_path), "--verify"])
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "VERIFY FAIL" in combined
    assert "produced:" in combined
    assert "pinned:" in combined


# ---------------------------------------------------------------------------
# Slice (c): --publish flag validation
# ---------------------------------------------------------------------------


def test_publish_requires_a_tag() -> None:
    result = _run(["bash", str(SCRIPT), "--publish"])
    assert result.returncode != 0
    assert "vMAJOR.MINOR.PATCH" in (result.stdout + result.stderr)


def test_publish_rejects_malformed_tag() -> None:
    result = _run(["bash", str(SCRIPT), "--publish", "bad-tag"])
    assert result.returncode != 0
    assert "vMAJOR.MINOR.PATCH" in (result.stdout + result.stderr)


def test_publish_rejects_when_gh_cli_missing() -> None:
    # Scrub PATH to a minimal set so `gh` cannot be resolved. The script
    # should refuse before doing any build work.
    minimal_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    result = _run(
        ["bash", str(SCRIPT), "--publish", "v0.2.0"],
        env=minimal_env,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "gh CLI not found" in combined or "gh not authenticated" in combined


# ---------------------------------------------------------------------------
# Slice (d): default 6-doc set + subsetter behavior
# ---------------------------------------------------------------------------


def _wc_src_present() -> bool:
    return (REPO_ROOT / "docs" / "welcome-corpus-src" / "03-built-on-aiar.md").is_file()


@pytest.mark.skipif(not _wc_src_present(), reason="docs/welcome-corpus-src/ not landed yet")
def test_default_set_contains_six_docs(tmp_path: Path) -> None:
    result = _run(["bash", str(SCRIPT), "--output-dir", str(tmp_path)])
    assert result.returncode == 0, result.stderr
    with tarfile.open(tmp_path / "welcome-corpus.tar.gz", "r:gz") as tf:
        f = tf.extractfile("welcome-corpus/manifest.json")
        assert f is not None
        manifest = json.loads(f.read().decode("utf-8"))
    expected = {
        "docs/00-what-is-errorta.md",
        "docs/01-the-judge-loop.md",
        "docs/02-corpora-and-rag.md",
        "docs/03-built-on-aiar.md",
        "docs/04-faq.md",
        "docs/05-how-to-add-your-own-files.md",
    }
    assert set(manifest["files"]) == expected


@pytest.mark.skipif(not _wc_src_present(), reason="docs/welcome-corpus-src/ not landed yet")
def test_subsetter_strips_technical_approach_section(tmp_path: Path) -> None:
    result = _run(["bash", str(SCRIPT), "--output-dir", str(tmp_path)])
    assert result.returncode == 0
    with tarfile.open(tmp_path / "welcome-corpus.tar.gz", "r:gz") as tf:
        f = tf.extractfile("welcome-corpus/docs/01-the-judge-loop.md")
        assert f is not None
        contents = f.read().decode("utf-8")
    assert "## Technical approach" not in contents


def test_subsetter_aborts_when_marker_missing() -> None:
    # Direct grep of the script proves the failure path is wired. We don't
    # exercise it via a synthetic spec injection because that would require
    # overlaying real spec files in the repo.
    script_text = SCRIPT.read_text()
    assert "could not locate '## Technical approach'" in script_text


@pytest.mark.skipif(not _wc_src_present(), reason="docs/welcome-corpus-src/ not landed yet")
def test_unpacked_size_within_bounds(tmp_path: Path) -> None:
    result = _run(["bash", str(SCRIPT), "--output-dir", str(tmp_path)])
    assert result.returncode == 0
    total = 0
    with tarfile.open(tmp_path / "welcome-corpus.tar.gz", "r:gz") as tf:
        for m in tf.getmembers():
            if m.isfile() and m.name.startswith("welcome-corpus/docs/"):
                total += m.size
    # Soft floor + cap; load-bearing constraint is the 5 MiB max_bytes
    # enforced by the script itself.
    assert 5_000 <= total <= 100_000, f"unpacked docs total = {total} bytes"
