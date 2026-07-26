"""Spec 19 anti-drift canary — version identity and build provenance.

Why this file exists: `0.1.0-alpha.11` shipped a binary that self-reported
`0.1.0-alpha.10`. The code was correct; only the string was stale, and it sent a
post-release investigation down a wrong path ("the CLI is still running old
code") that ended with a hand-run sidecar squatting the desktop app's port.

The Python lineage declares its version in three places and nothing asserted
they agree:

  * ``python/pyproject.toml``            — canonical; ``scripts/release-cli.sh``
    seds it for the git tag, the tarball name and the Homebrew formula;
  * ``errorta_app.__version__``          — ``/healthz``, ``/version``, the
    FastAPI app ``version=`` (server.py:450/635/664);
  * ``errorta_cli.__version__``          — the CLI's own self-report.

Deliberately NOT asserted here: the installed dist-info metadata. The editable
install in the build venv reports ``0.1.0a0`` — a fourth value that has nothing
to do with what ships — so asserting against ``importlib.metadata`` would fail
CI for an unrelated reason (Spec 19, Edge cases).

Companion locks: ``scripts/release-cli.sh``'s preflight dies on drift (Item 3),
and ``scripts/bump-version.sh`` is the only supported way to change the value.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from errorta_app import build_info as bi

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "python" / "pyproject.toml"
_APP_INIT = _REPO_ROOT / "python" / "errorta_app" / "__init__.py"
_CLI_INIT = _REPO_ROOT / "python" / "errorta_cli" / "__init__.py"
_QUERY_INIT = _REPO_ROOT / "python" / "errorta_query" / "__init__.py"
_RELEASE_CLI = _REPO_ROOT / "scripts" / "release-cli.sh"
_BUMP_SCRIPT = _REPO_ROOT / "scripts" / "bump-version.sh"
_CLI_SPEC = _REPO_ROOT / "python" / "cli.spec"

# The exact sed program in scripts/release-cli.sh (version resolution) — the release pipeline's
# ONLY version source. Locking the literal here means a rewrite of that line
# fails this test instead of silently producing an unversioned release.
_SED_PROGRAM = r's/^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p'

# Python transliteration of _SED_PROGRAM. POSIX [[:space:]] applied line-wise is
# the horizontal-whitespace class (sed never sees the newline).
_DECL_RE = re.compile(r'^[ \t\f\v\r]*(?P<lhs>[A-Za-z_][A-Za-z0-9_]*)'
                      r'[ \t\f\v\r]*=[ \t\f\v\r]*"(?P<value>[^"]*)".*')

_FIX = "run `bash scripts/bump-version.sh X.Y.Z` (Spec 19) — never hand-edit one declaration"


def _first_decl(path: Path, lhs: str) -> str | None:
    """First ``<lhs> = "value"`` literal in *path* — the `| head -1` semantics."""
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _DECL_RE.match(line)
        if m is not None and m.group("lhs") == lhs:
            return m.group("value")
    return None


# --------------------------------------------------------------------------- #
# Item 1 — one canonical version, mirrors asserted equal
# --------------------------------------------------------------------------- #

def test_three_version_declarations_are_byte_equal() -> None:
    canonical = _first_decl(_PYPROJECT, "version")
    app = _first_decl(_APP_INIT, "__version__")
    cli = _first_decl(_CLI_INIT, "__version__")

    assert canonical, f"no `version = \"...\"` in {_PYPROJECT} — {_FIX}"
    assert app == canonical, (
        f"VERSION DRIFT: python/errorta_app/__init__.py says {app!r} but "
        f"python/pyproject.toml says {canonical!r}. The shipped sidecar would "
        f"self-report a version it was not released as (/healthz, /version). "
        f"Fix: {_FIX} — e.g. `bash scripts/bump-version.sh {canonical}`."
    )
    assert cli == canonical, (
        f"VERSION DRIFT: python/errorta_cli/__init__.py says {cli!r} but "
        f"python/pyproject.toml says {canonical!r}. `errorta status` would "
        f"self-report a version it was not released as. "
        f"Fix: {_FIX} — e.g. `bash scripts/bump-version.sh {canonical}`."
    )
    # errorta_query ships in the same wheel/binary (pyproject `include =
    # ["errorta_*"]`). Nothing reads its literal today — which is exactly why it
    # silently drifted to alpha.10 while the others moved; lock it before some
    # future health payload starts reporting it.
    qry = _first_decl(_QUERY_INIT, "__version__")
    assert qry == canonical, (
        f"VERSION DRIFT: python/errorta_query/__init__.py says {qry!r} but "
        f"python/pyproject.toml says {canonical!r}. It ships in the same "
        f"binary. Fix: {_FIX} — e.g. `bash scripts/bump-version.sh {canonical}`."
    )


def test_imported_dunder_versions_match_the_source_literals() -> None:
    """The literals are what the *modules* export — no lazy/computed override."""
    import errorta_app
    import errorta_cli

    canonical = _first_decl(_PYPROJECT, "version")
    assert errorta_app.__version__ == canonical, (
        f"errorta_app.__version__ ({errorta_app.__version__!r}) != pyproject "
        f"({canonical!r}). Fix: {_FIX}."
    )
    assert errorta_cli.__version__ == canonical, (
        f"errorta_cli.__version__ ({errorta_cli.__version__!r}) != pyproject "
        f"({canonical!r}). Fix: {_FIX}."
    )


def test_release_cli_sed_program_is_unchanged() -> None:
    """release-cli.sh still carries the exact regex this test replicates."""
    text = _RELEASE_CLI.read_text(encoding="utf-8")
    assert _SED_PROGRAM in text, (
        "scripts/release-cli.sh no longer contains the expected version-extraction "
        f"sed program:\n  {_SED_PROGRAM}\nIf it was intentionally changed, update "
        "_SED_PROGRAM/_DECL_RE here in lockstep — the release pipeline reads the "
        "version from pyproject.toml with that program and nothing else."
    )


def test_release_cli_sed_extracts_the_canonical_version() -> None:
    """Run the REAL sed program against the REAL pyproject — no transliteration
    gap. Also proves pyproject stayed sed-readable (Spec 19 Item 1 acceptance)."""
    out = subprocess.run(
        ["sed", "-n", _SED_PROGRAM, str(_PYPROJECT)],
        capture_output=True, text=True, check=True, timeout=15,
    ).stdout.splitlines()
    assert out, "release-cli.sh's sed extracted NOTHING from pyproject.toml"
    assert out[0] == _first_decl(_PYPROJECT, "version"), (
        f"release-cli.sh would tag/name the release {out[0]!r} while the "
        f"declared version is {_first_decl(_PYPROJECT, 'version')!r}."
    )


def test_bump_script_exists_and_is_executable() -> None:
    """The failure messages above name it; it must actually be runnable."""
    assert _BUMP_SCRIPT.is_file(), "scripts/bump-version.sh is missing (Spec 19 Item 2)"
    import os
    assert os.access(_BUMP_SCRIPT, os.X_OK), "scripts/bump-version.sh is not executable"


# --------------------------------------------------------------------------- #
# Item 3 — the release preflight gate exists
# --------------------------------------------------------------------------- #

def test_release_cli_preflight_gates_on_version_identity() -> None:
    text = _RELEASE_CLI.read_text(encoding="utf-8")
    assert "check_version_identity()" in text, (
        "scripts/release-cli.sh lost its check_version_identity() gate (Spec 19 Item 3)"
    )
    # Called from preflight (--check) AND from the build path (so --dry-run is
    # gated too): two call sites plus the definition.
    assert text.count("check_version_identity") >= 3, (
        "check_version_identity must be invoked by BOTH preflight() and the build "
        "path — a --dry-run that previews a mislabeled artifact is not a dry-run."
    )
    assert "bash scripts/bump-version.sh" in text, (
        "the drift failure must name the fix command"
    )


# --------------------------------------------------------------------------- #
# Item 4 — build provenance stamped into the CLI binary
# --------------------------------------------------------------------------- #

_STAMP_KEYS = {"commit", "built_at", "dirty", "source"}


def _fresh() -> None:
    bi.build_info.cache_clear()


def test_release_cli_stamps_the_exact_build_info_shape() -> None:
    """The printf in release-cli.sh must emit exactly what _from_bundle() reads."""
    text = _RELEASE_CLI.read_text(encoding="utf-8")
    assert '"source":"release-cli"' in text, (
        "scripts/release-cli.sh no longer stamps source=release-cli (Spec 19 Item 4)"
    )
    for key in _STAMP_KEYS:
        assert f'"{key}"' in text, f"_build_info.json stamp is missing the {key!r} key"
    # Written before pyinstaller, removed after — via a trap, so an interrupted
    # build cannot leave a stale stamp poisoning the next source-checkout run.
    assert "_build_info.json" in text and "trap cleanup_build_info" in text, (
        "the _build_info.json stamp must be cleaned up by a trap, not only on the "
        "happy path"
    )


def test_cli_spec_bundles_build_info_anchored_to_the_spec_dir() -> None:
    """release-cli.sh runs pyinstaller from the REPO ROOT (build-sidecar.sh cds
    into python/ first), so a cwd-relative existence probe would silently be
    False and every released binary would fall back to provenance 'unknown'."""
    text = _CLI_SPEC.read_text(encoding="utf-8")
    assert '("errorta_app/_build_info.json", "errorta_app")' in text, (
        "python/cli.spec no longer bundles errorta_app/_build_info.json as data"
    )
    assert "SPECPATH" in text, (
        "python/cli.spec's _build_info.json probe must be anchored to SPECPATH, "
        "not the cwd — release-cli.sh invokes pyinstaller from the repo root."
    )
    assert "_build_info_datas" in text and "] + _build_info_datas" in text


def test_stamped_build_info_is_reported(monkeypatch, tmp_path) -> None:
    """A binary built by release-cli.sh reports the real commit, not null."""
    stamp = tmp_path / "_build_info.json"
    stamp.write_text(json.dumps({
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "built_at": "2026-07-25T12:00:00Z",
        "dirty": False,
        "source": "release-cli",
    }), encoding="utf-8")
    # Stand in for sys._MEIPASS/errorta_app/_build_info.json.
    monkeypatch.setattr(bi, "_bundled_paths", lambda: [stamp])
    monkeypatch.setenv("ERRORTA_BUILD_COMMIT", "env-must-not-win")
    _fresh()
    try:
        info = bi.build_info()
        assert info["source"] == "release-cli"
        assert info["commit"] == "0123456789abcdef0123456789abcdef01234567"
        assert info["commit_short"] == "0123456789ab"
        assert info["dirty"] is False
        assert info["built_at"] == "2026-07-25T12:00:00Z"
        assert set(_STAMP_KEYS) <= set(info)
    finally:
        _fresh()


def test_dirty_release_stamp_is_reported_not_refused(monkeypatch, tmp_path) -> None:
    """Provenance describes reality: a dirty build stamps dirty:true."""
    stamp = tmp_path / "_build_info.json"
    stamp.write_text(json.dumps({
        "commit": "feedfacefeedfacefeedface", "built_at": "2026-07-25T12:00:00Z",
        "dirty": True, "source": "release-cli",
    }), encoding="utf-8")
    monkeypatch.setattr(bi, "_bundled_paths", lambda: [stamp])
    _fresh()
    try:
        info = bi.build_info()
        assert info["dirty"] is True and info["source"] == "release-cli"
    finally:
        _fresh()


def test_missing_stamp_in_a_frozen_binary_fails_open(monkeypatch, tmp_path) -> None:
    """The fail-open lock: an UNSTAMPED frozen binary must degrade to 'unknown'
    without raising. (_from_git deliberately returns None when sys.frozen, so
    there is no tier-3 rescue — this is exactly the alpha.11 situation.)"""
    monkeypatch.setattr(bi, "_bundled_paths", lambda: [tmp_path / "_build_info.json"])
    monkeypatch.delenv("ERRORTA_BUILD_COMMIT", raising=False)
    monkeypatch.delenv("ERRORTA_BUILT_AT", raising=False)
    monkeypatch.delenv("ERRORTA_BUILD_DIRTY", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _fresh()
    try:
        info = bi.build_info()  # must not raise
        assert info["source"] == "unknown"
        assert info["commit"] is None and info["commit_short"] is None
        assert info["dirty"] is False and info["built_at"] is None
    finally:
        monkeypatch.undo()
        _fresh()


@pytest.mark.parametrize("bad", ["", "{ not json", '{"commit": null}', "[]"])
def test_malformed_stamp_is_ignored_not_fatal(monkeypatch, tmp_path, bad) -> None:
    stamp = tmp_path / "_build_info.json"
    stamp.write_text(bad, encoding="utf-8")
    monkeypatch.setattr(bi, "_bundled_paths", lambda: [stamp])
    assert bi._from_bundle() is None
