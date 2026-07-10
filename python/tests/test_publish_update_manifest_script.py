"""F-INFRA-09 Slice 3 — argv smoke tests for publish-update-manifest.sh.

The script signs an artifact + pushes a Tauri-v2 manifest to errorta-downloads
and uploads a .sig companion to a GitHub Release. None of that runs here:
this module only checks the arg-parsing layer + missing-input error paths.

Per project policy, GitHub Actions stays OFF; the script is maintainer-only.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "publish-update-manifest.sh"


def _run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    real_env = os.environ.copy()
    if env:
        real_env.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=real_env,
        cwd=str(REPO_ROOT),
    )


def test_script_is_executable_and_present() -> None:
    assert SCRIPT.exists(), f"missing script: {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"not executable: {SCRIPT}"


def test_no_args_prints_usage_and_exits_nonzero() -> None:
    result = _run([])
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "Usage:" in combined or "usage" in combined.lower()
    assert "version-tag" in combined or "<version-tag>" in combined


def test_help_flag_exits_zero() -> None:
    result = _run(["--help"])
    assert result.returncode == 0
    assert "Usage:" in result.stdout


def test_missing_platform_exits_nonzero() -> None:
    result = _run(["v0.5.0"])
    assert result.returncode != 0
    assert "platform" in result.stderr.lower()


def test_invalid_platform_exits_nonzero() -> None:
    result = _run([
        "v0.5.0",
        "--platform", "freebsd-riscv",
        "--artifact-url", "https://example.com/x.tar.gz",
        "--artifact-local", "/dev/null",
        "--notes-file", "/dev/null",
        "--key-path", "/dev/null",
    ])
    assert result.returncode != 0
    assert "platform" in result.stderr.lower()


def test_non_https_artifact_url_exits_nonzero(tmp_path: Path) -> None:
    art = tmp_path / "a.tar.gz"
    art.write_bytes(b"x")
    notes = tmp_path / "notes.md"
    notes.write_text("release notes")
    key = tmp_path / "key"
    key.write_text("dummy")
    result = _run([
        "v0.5.0",
        "--platform", "darwin-aarch64",
        "--artifact-url", "http://example.com/x.tar.gz",
        "--artifact-local", str(art),
        "--notes-file", str(notes),
        "--key-path", str(key),
        "--dry-run",
    ])
    assert result.returncode != 0
    assert "https" in result.stderr.lower()


def test_missing_artifact_local_exits_nonzero(tmp_path: Path) -> None:
    notes = tmp_path / "notes.md"
    notes.write_text("release notes")
    key = tmp_path / "key"
    key.write_text("dummy")
    bogus = tmp_path / "definitely-not-here.tar.gz"
    result = _run([
        "v0.5.0",
        "--platform", "darwin-aarch64",
        "--artifact-url", "https://example.com/x.tar.gz",
        "--artifact-local", str(bogus),
        "--notes-file", str(notes),
        "--key-path", str(key),
        "--dry-run",
    ])
    assert result.returncode != 0
    assert str(bogus) in result.stderr or "artifact-local" in result.stderr.lower()


def test_draft_and_no_draft_are_mutually_exclusive(tmp_path: Path) -> None:
    art = tmp_path / "a.tar.gz"
    art.write_bytes(b"x")
    notes = tmp_path / "notes.md"
    notes.write_text("release notes")
    key = tmp_path / "key"
    key.write_text("dummy")
    result = _run([
        "v0.5.0",
        "--platform", "darwin-aarch64",
        "--artifact-url", "https://example.com/x.tar.gz",
        "--artifact-local", str(art),
        "--notes-file", str(notes),
        "--key-path", str(key),
        "--draft",
        "--no-draft",
        "--dry-run",
    ])
    assert result.returncode != 0
    assert "mutually exclusive" in result.stderr.lower()


def test_dry_run_with_full_args_succeeds(tmp_path: Path) -> None:
    art = tmp_path / "a.tar.gz"
    art.write_bytes(b"x")
    notes = tmp_path / "notes.md"
    notes.write_text("release notes")
    key = tmp_path / "key"
    key.write_text("dummy")
    result = _run([
        "v0.5.0",
        "--platform", "darwin-aarch64",
        "--artifact-url", "https://example.com/x.tar.gz",
        "--artifact-local", str(art),
        "--notes-file", str(notes),
        "--key-path", str(key),
        "--dry-run",
    ])
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert "dry-run" in result.stdout.lower()
