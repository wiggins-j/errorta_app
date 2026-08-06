"""F154 — a zero-config compile floor for test-less projects.

A greenfield project starts with an EMPTY test-command registry, and two gates read
that emptiness as success: ``_set_mergeable_if_ready``'s ``tests_ok`` is vacuously
satisfied (so every PR merges on a reviewer model-approval with zero compilation
ever run), and ``delivery_review`` sets ``tests_passed=True`` unconditionally. F152
and F153 catch a runnable app that fails to *serve or start*; they cannot catch a
compile or type error on a code path never requested at launch.

So: when the registry is empty, the delivery review derives a build/typecheck
command from the detected stack and treats its failure like a failed test. ``None``
for any stack without a safe rule, which preserves today's behaviour exactly.
"""
import sys
from pathlib import Path

from errorta_council.coding.autonomy import (
    CADENCE_OFF,
    CodingAutonomyPolicy,
    policy_from_dict,
    policy_to_dict,
)
from errorta_council.coding.runner import _default_verify_command


# --------------------------------------------------------------------------- #
# The resolver — a small, conservative table. `None` is always the safe answer.
# --------------------------------------------------------------------------- #
def test_derives_npm_build_when_build_script_present(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"build": "vite build"}}')
    argv, cwd = _default_verify_command(tmp_path)
    assert argv == ["npm", "run", "build"]
    assert cwd == tmp_path


def test_derives_tsc_noemit_when_no_build_script(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"dev": "vite"}}')
    (tmp_path / "tsconfig.json").write_text("{}")
    argv, cwd = _default_verify_command(tmp_path)
    assert argv == ["npx", "--no-install", "tsc", "--noEmit"]
    assert cwd == tmp_path


def test_package_json_without_build_or_tsconfig_is_none(tmp_path: Path) -> None:
    # Nothing safe to run — do NOT invent a build.
    (tmp_path / "package.json").write_text('{"scripts": {"dev": "vite"}}')
    assert _default_verify_command(tmp_path) is None


def test_derives_compileall_for_python(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n")
    argv, cwd = _default_verify_command(tmp_path)
    assert argv[:3] == [sys.executable, "-m", "compileall"]
    assert cwd == tmp_path


def test_derives_cargo_build(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    argv, _ = _default_verify_command(tmp_path)
    assert argv == ["cargo", "build", "--quiet"]


def test_derives_go_build(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")
    argv, _ = _default_verify_command(tmp_path)
    assert argv == ["go", "build", "./..."]


def test_unknown_stack_is_none(tmp_path: Path) -> None:
    # A static HTML site has no compile step to fail — F152/F153 still gate it if
    # it is served. `None` preserves today's vacuous-clean exactly.
    (tmp_path / "index.html").write_text("<!doctype html><p>hi")
    assert _default_verify_command(tmp_path) is None


def test_malformed_package_json_is_none(tmp_path: Path) -> None:
    # Never raise into the delivery gate, and never guess a build for a manifest we
    # could not read — a false build finding is worse than no gate.
    (tmp_path / "package.json").write_text("{not json")
    assert _default_verify_command(tmp_path) is None


def test_subdir_manifest_sets_cwd(tmp_path: Path) -> None:
    # The CLI delivers into a subdir; cwd must follow the manifest, not the root.
    app = tmp_path / "app"
    app.mkdir()
    (app / "package.json").write_text('{"scripts": {"build": "next build"}}')
    argv, cwd = _default_verify_command(tmp_path)
    assert argv == ["npm", "run", "build"]
    assert cwd == app


def test_node_wins_over_python_in_a_polyglot_tree(tmp_path: Path) -> None:
    # Order matters: a real build is a strictly stronger check than compileall,
    # which only catches syntax errors.
    (tmp_path / "package.json").write_text('{"scripts": {"build": "vite build"}}')
    (tmp_path / "tool.py").write_text("x = 1\n")
    argv, _ = _default_verify_command(tmp_path)
    assert argv == ["npm", "run", "build"]


def test_missing_root_is_none(tmp_path: Path) -> None:
    assert _default_verify_command(tmp_path / "nope") is None


def test_default_build_gate_knob_roundtrips() -> None:
    p = CodingAutonomyPolicy()
    assert p.default_build_gate is True
    d = policy_to_dict(p)
    assert d["default_build_gate"] is True
    assert policy_from_dict({**d, "default_build_gate": False}
                            ).default_build_gate is False
