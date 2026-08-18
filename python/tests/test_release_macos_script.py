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
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "release-macos.sh"


NOTARIZE_LIB_PATH = REPO_ROOT / "scripts" / "lib" / "notarize.sh"
VERIFY_LIB_PATH = REPO_ROOT / "scripts" / "lib" / "verify-release.sh"


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def notarize_text() -> str:
    """``scripts/lib/notarize.sh`` — where submit/staple actually live.

    The notarize + staple implementation was extracted out of
    ``release-macos.sh`` into this sourced library so ``release-cli.sh`` can
    reuse it. Assertions about HOW notarization works belong here; assertions
    that the release script still WIRES it up belong on ``script_text`` (see
    ``test_script_wires_up_the_notarize_library``).
    """
    return NOTARIZE_LIB_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def verify_text() -> str:
    """``scripts/lib/verify-release.sh`` — the post-notarize self-verify."""
    return VERIFY_LIB_PATH.read_text(encoding="utf-8")


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


def test_missing_apple_signing_identity_fails(tmp_path: Path) -> None:
    """With a tag arg but no env file and no APPLE_SIGNING_IDENTITY,
    the script must fail at the identity guard.

    Runs the script from a pristine throwaway git repo rather than the real
    checkout. The script's clean-HEAD guard (`git status --porcelain`) runs
    BEFORE the identity guard, so against the developer's own tree this test
    failed with "working tree is dirty" whenever ANY file was uncommitted —
    i.e. exactly when you normally run the suite. The temp repo makes the
    test depend only on the script, never on the state of the tree it lives in.
    """
    repo = tmp_path / "repo"
    (repo / "scripts" / "lib").mkdir(parents=True)
    shutil.copy2(SCRIPT_PATH, repo / "scripts" / SCRIPT_PATH.name)
    for lib in (NOTARIZE_LIB_PATH, VERIFY_LIB_PATH):
        shutil.copy2(lib, repo / "scripts" / "lib" / lib.name)

    git_env = {
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ.get("PATH", ""),
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    os.makedirs(git_env["HOME"], exist_ok=True)
    for args in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "seed"],
    ):
        subprocess.run(args, cwd=repo, env=git_env, check=True,
                       capture_output=True, text=True)

    result = subprocess.run(
        ["bash", str(repo / "scripts" / SCRIPT_PATH.name), "v0.0.0-test"],
        capture_output=True, text=True, check=False, cwd=repo, env=git_env,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "working tree is dirty" not in output, (
        "the temp repo must be clean so the identity guard is what fires"
    )
    assert "APPLE_SIGNING_IDENTITY" in output


def test_script_wires_up_the_notarize_library(script_text: str) -> None:
    """The release script must SOURCE the notarize/verify libs and CALL them.

    The submit/staple/spctl assertions below moved to the lib files when that
    logic was extracted. Without this test they could all pass while
    release-macos.sh quietly stopped notarizing at all — the guard has to
    cover the wiring, not just the implementation.
    """
    assert "scripts/lib/notarize.sh" in script_text
    assert "scripts/lib/verify-release.sh" in script_text
    for fn in ("notarize_app", "notarize_and_staple", "verify_release"):
        assert re.search(rf"^\s*{fn} ", script_text, re.MULTILINE), (
            f"release-macos.sh must call {fn} — sourcing the lib without "
            "invoking it would ship an unnotarized build"
        )


def test_notarize_lib_references_notarytool_submit(notarize_text: str) -> None:
    # Exactly two notarytool submit invocations — one in the keychain-profile
    # branch, one in the env-var fallback. Only one runs per invocation; the
    # if/else in _notary_submit gates them.
    submits = re.findall(r"xcrun notarytool submit", notarize_text)
    assert len(submits) == 2, (
        "scripts/lib/notarize.sh must contain exactly two `xcrun notarytool "
        "submit` invocations (keychain-profile + env-var branches); "
        f"found {len(submits)}"
    )


def test_notarize_lib_detects_the_keychain_profile_by_liveness_probe(
    notarize_text: str,
) -> None:
    """Credential detection uses a LIVENESS PROBE, not a service-name query.

    The old check was `security find-generic-password -s
    "com.apple.gke.notary.tool"`, which reports MISSING even when the profile
    exists and works — the bug that made release-macos.sh fall through to env
    vars and fail (see the header comment in scripts/lib/notarize.sh). This
    test guards the replacement AND guards against the brittle probe coming
    back.
    """
    assert 'xcrun notarytool history --keychain-profile "$ERRORTA_NOTARY_PROFILE"' in (
        notarize_text
    ), (
        "notarize.sh must detect the stored profile with a `notarytool "
        "history` liveness probe"
    )
    assert 'ERRORTA_NOTARY_PROFILE:-errorta-notary' in notarize_text, (
        "the default notarytool keychain profile must be errorta-notary"
    )
    # Strip comments first — the header comment legitimately NAMES the
    # abandoned probe to explain why it was abandoned. Only executable code
    # must be free of it.
    code = "\n".join(
        ln for ln in notarize_text.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "find-generic-password" not in code, (
        "the brittle `security find-generic-password` profile probe must NOT "
        "come back — it reports MISSING for a working profile"
    )


def test_notarize_lib_keeps_env_var_fallback(notarize_text: str) -> None:
    """The env-var notarize path must remain as the fallback when the
    keychain profile is absent."""
    assert "--apple-id" in notarize_text
    assert re.search(r"\$\{?APPLE_ID\}?", notarize_text), (
        "the env-var branch must pass $APPLE_ID"
    )
    for var in ("APPLE_TEAM_ID", "APPLE_APP_SPECIFIC_PASSWORD"):
        assert var in notarize_text


def test_notarize_branches_inside_if_else(notarize_text: str) -> None:
    """Regression guard: both `xcrun notarytool submit` invocations must live
    inside the same if/else block — never on the happy path together."""
    if_match = re.search(
        r'if \[\[ "\$mode" == "profile" \]\].*?\n  fi',
        notarize_text,
        re.DOTALL,
    )
    assert if_match is not None, (
        "notarize.sh must wrap both notarize branches in a single "
        '`if [[ "$mode" == "profile" ]] … fi` block.'
    )
    submits_in_block = re.findall(r"xcrun notarytool submit", if_match.group(0))
    assert len(submits_in_block) == 2, (
        "both notarize-submit invocations must live inside the same "
        f"if/else block; found {len(submits_in_block)} inside the block"
    )


def test_notarize_lib_references_staple(notarize_text: str) -> None:
    assert "xcrun stapler staple" in notarize_text
    assert "xcrun stapler validate" in notarize_text


def test_verify_lib_runs_spctl_assess(verify_text: str) -> None:
    assert "spctl --assess" in verify_text


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
