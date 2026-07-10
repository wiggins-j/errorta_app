"""Tests for scripts/publish-linux-release.sh.

Slice 4 of F-INFRA-07. Exercises:
- argument parsing (no args, malformed tag)
- missing-artifact error path

The `gh release` happy path is NOT unit-testable here (requires
network + auth). It is exercised by the maintainer per the plan's
"Real end-to-end verification" section.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "publish-linux-release.sh"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK), "publish-linux-release.sh should be executable"


def test_no_args_exits_non_zero_with_usage() -> None:
    result = _run(["bash", str(SCRIPT)])
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).lower()
    assert "usage" in combined or "version-tag" in combined


def test_help_flag_prints_usage() -> None:
    result = _run(["bash", str(SCRIPT), "--help"])
    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "<version-tag>" in result.stdout
    assert "--no-draft" in result.stdout


def test_malformed_tag_rejected() -> None:
    result = _run(["bash", str(SCRIPT), "not-a-tag"])
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "vMAJOR.MINOR.PATCH" in combined or "does not look like" in combined


def test_missing_artifacts_reports_both_paths() -> None:
    # Valid tag shape, no artifacts on disk in this checkout.
    result = _run(["bash", str(SCRIPT), "v0.4.0-test"])
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "missing Linux build artifacts" in combined
    assert "AppImage" in combined
    assert ".deb" in combined


def test_repo_target_is_errorta_downloads() -> None:
    # Quick grep: the script must publish to the public downloads repo,
    # not to the source repo wiggins-j/errorta_app.
    text = SCRIPT.read_text()
    assert "wiggins-j/errorta-downloads" in text
    assert "wiggins-j/errorta_app\"" not in text  # never the source repo
