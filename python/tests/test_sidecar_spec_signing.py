"""F-INFRA-02 Slice (c) — assert the PyInstaller spec exposes the
signing env-var seam.

The real PyInstaller build is gated on macOS + a clean venv, so this
suite stays filesystem-only: it reads sidecar.spec as text and asserts
the two env-var lookups + the import + the regression guard against
the prior bare ``codesign_identity=None`` literal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SPEC_PATH = Path(__file__).resolve().parents[1] / "sidecar.spec"


@pytest.fixture(scope="module")
def spec_text() -> str:
    return SPEC_PATH.read_text(encoding="utf-8")


def test_spec_file_exists() -> None:
    assert SPEC_PATH.is_file(), f"sidecar.spec missing at {SPEC_PATH}"


def test_spec_imports_os(spec_text: str) -> None:
    assert "import os" in spec_text, (
        "sidecar.spec must `import os` so the env-var hooks resolve "
        "at PyInstaller spec-evaluation time (F-INFRA-02 Slice c)."
    )


def test_spec_reads_codesign_identity_from_env(spec_text: str) -> None:
    assert 'os.environ.get("ERRORTA_CODESIGN_IDENTITY")' in spec_text, (
        "sidecar.spec must read ERRORTA_CODESIGN_IDENTITY via "
        "os.environ.get so the release script can drive signing without "
        "edits (F-INFRA-02 Slice c)."
    )


def test_spec_reads_entitlements_path_from_env(spec_text: str) -> None:
    assert 'os.environ.get("ERRORTA_ENTITLEMENTS_PLIST")' in spec_text, (
        "sidecar.spec must read ERRORTA_ENTITLEMENTS_PLIST via "
        "os.environ.get so the entitlements file path is env-driven "
        "(F-INFRA-02 Slice c)."
    )


def test_spec_no_bare_codesign_identity_none(spec_text: str) -> None:
    # Regression guard: the prior literal was `codesign_identity=None,`.
    # The env-var form must not regress to a bare None.
    assert "codesign_identity=None" not in spec_text, (
        "sidecar.spec must not contain the bare `codesign_identity=None` "
        "literal; signing identity is env-driven via "
        "ERRORTA_CODESIGN_IDENTITY (F-INFRA-02 Slice c)."
    )


def test_spec_bundles_runtime_json_data_files(spec_text: str) -> None:
    """Packaged sidecar must include JSON data loaded via Path(__file__)."""
    assert '("errorta_hwdetect/recommendations.json", "errorta_hwdetect")' in spec_text
    assert '("errorta_ollama/known_hashes.json", "errorta_ollama")' in spec_text
    assert '("errorta_welcome/pinned_hash.json", "errorta_welcome")' in spec_text


def test_pyproject_includes_runtime_packages_and_data() -> None:
    """Release wheels must carry the same runtime packages/data as PyInstaller."""
    pyproject = (SPEC_PATH.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert 'include = ["errorta_app*", "errorta_*"]' in pyproject
    assert 'errorta_hwdetect = ["recommendations.json"]' in pyproject
    assert 'errorta_ollama = ["known_hashes.json"]' in pyproject
    assert 'errorta_welcome = ["pinned_hash.json"]' in pyproject
