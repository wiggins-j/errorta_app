"""F-INFRA-02 Slices (d) + (e) — hermetic shell-only checks of
scripts/release-macos.sh.

These tests never invoke the real Tauri build, xcrun notarytool, or
codesign — those need macOS + a real cert + Apple credentials. The
suite verifies the script's argument parsing, required-var guards,
executable bit, regression-guards against committed identity strings,
and the structural shape of the notarize branches.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "release-macos.sh"


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def test_script_exists() -> None:
    assert SCRIPT_PATH.is_file(), f"missing {SCRIPT_PATH}"


def test_script_is_executable() -> None:
    assert os.access(SCRIPT_PATH, os.X_OK), (
        f"{SCRIPT_PATH} must be executable (chmod +x)"
    )


def test_script_has_set_euo_pipefail(script_text: str) -> None:
    assert "set -euo pipefail" in script_text, (
        "release-macos.sh must `set -euo pipefail` for fail-fast"
    )


def test_script_syntax_valid() -> None:
    # bash -n is a parse-only check; safe to run on any platform with bash.
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"bash -n failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )


def test_no_arg_fails_with_usage() -> None:
    """Calling the script with no positional tag must exit non-zero
    and surface the `usage:` text. Stops short of the build by failing
    at the `${1:?...}` gate.
    """
    result = subprocess.run(
        ["bash", str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        check=False,
        # Hermetic env: drop any APPLE_* the maintainer might have exported.
        env={"HOME": "/tmp/release-macos-test-home", "PATH": os.environ.get("PATH", "")},
    )
    assert result.returncode != 0
    assert "usage" in (result.stdout + result.stderr).lower()


def test_missing_apple_signing_identity_fails() -> None:
    """With a tag arg but no env file and no APPLE_SIGNING_IDENTITY,
    the script must fail at the identity guard.
    """
    fake_home = "/tmp/release-macos-test-home-no-env"
    os.makedirs(fake_home, exist_ok=True)
    # Ensure no env file is found there.
    env_file = os.path.join(fake_home, ".config", "errorta-release.env")
    if os.path.exists(env_file):
        os.remove(env_file)
    result = subprocess.run(
        ["bash", str(SCRIPT_PATH), "v0.0.0-test"],
        capture_output=True,
        text=True,
        check=False,
        env={"HOME": fake_home, "PATH": os.environ.get("PATH", "")},
    )
    assert result.returncode != 0
    assert "APPLE_SIGNING_IDENTITY" in (result.stdout + result.stderr)


def test_script_references_notarytool_submit(script_text: str) -> None:
    # Slice (e): exactly two notarytool submit invocations — one in the
    # keychain-profile branch, one in the env-var fallback. Only one
    # runs per invocation; the if/else gates them.
    submits = re.findall(r"xcrun notarytool submit", script_text)
    assert len(submits) == 2, (
        "release-macos.sh must contain exactly two `xcrun notarytool "
        "submit` invocations (keychain-profile + env-var branches); "
        f"found {len(submits)}"
    )


def test_script_has_keychain_profile_detection(script_text: str) -> None:
    """Slice (e): the script probes the login keychain for the stored
    notarytool profile before falling back to env-var credentials."""
    assert 'find-generic-password -s "com.apple.gke.notary.tool"' in script_text, (
        "release-macos.sh must probe security find-generic-password "
        "with service com.apple.gke.notary.tool to detect the stored "
        "notarytool keychain profile (F-INFRA-02 Slice e)."
    )
    assert "--keychain-profile errorta-notary" in script_text, (
        "release-macos.sh must invoke notarytool with "
        "--keychain-profile errorta-notary in the preferred branch."
    )


def test_script_keeps_env_var_fallback(script_text: str) -> None:
    """The env-var notarize path (Slice d) must remain as the fallback
    when the keychain profile is absent."""
    assert "--apple-id" in script_text
    assert "${APPLE_ID}" in script_text or '"${APPLE_ID}"' in script_text


def test_script_notarize_branches_inside_if_else(script_text: str) -> None:
    """Regression guard: both `xcrun notarytool submit` invocations
    must live inside the same if/else block — they must not be on the
    happy path together."""
    # Find the if-block bounds by anchoring on the detection probe.
    if_match = re.search(
        r'if security find-generic-password -s "com\.apple\.gke\.notary\.tool".*?\nfi',
        script_text,
        re.DOTALL,
    )
    assert if_match is not None, (
        "release-macos.sh must wrap both notarize branches in a single "
        "`if security find-generic-password … fi` block."
    )
    submits_in_block = re.findall(r"xcrun notarytool submit", if_match.group(0))
    assert len(submits_in_block) == 2, (
        "both notarize-submit invocations must live inside the same "
        f"if/else block; found {len(submits_in_block)} inside the block"
    )


def test_script_references_staple(script_text: str) -> None:
    assert "xcrun stapler staple" in script_text
    assert "xcrun stapler validate" in script_text


def test_script_runs_spctl_assess(script_text: str) -> None:
    assert "spctl --assess" in script_text


def test_script_exports_entitlements_path(script_text: str) -> None:
    assert "ERRORTA_ENTITLEMENTS_PLIST" in script_text
    assert "src-tauri/macos/entitlements.plist" in script_text


def test_script_does_not_contain_team_id_literal(script_text: str) -> None:
    """OPSEC regression guard: the maintainer's real Team ID must
    never land in the repo. The identity is env-driven.
    """
    # Apple Team IDs are 10-char alphanumeric. Allow lowercase 'macos'
    # / 'macOS' but reject any 10-char all-uppercase-or-digit token
    # adjacent to "Team ID" wording or inside a Developer ID Application
    # quoted string.
    forbidden_patterns = [
        r"Developer ID Application:\s*[A-Za-z]+\s+[A-Za-z]+\s*\([A-Z0-9]{10}\)",
        r"--team-id\s+[A-Z0-9]{10}",  # reject any hardcoded 10-char Team ID (env-driven)
    ]
    for pat in forbidden_patterns:
        assert not re.search(pat, script_text), (
            f"release-macos.sh must not contain the literal identity "
            f"pattern {pat!r}; identity is env-driven."
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only smoke")
def test_script_real_build_smoke() -> None:
    """Placeholder for the maintainer-Mac end-to-end smoke. Runs only
    on darwin; skips otherwise. Intentionally empty so it does not
    invoke the real build during automation."""
    pytest.skip("manual smoke test; run `bash scripts/release-macos.sh <tag>` directly")
