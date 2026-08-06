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


# --------------------------------------------------------------------------- #
# The gate itself — the build blocks `done`, end to end
# --------------------------------------------------------------------------- #
import json  # noqa: E402
import re  # noqa: E402

from errorta_council.coding.autonomy import save_policy  # noqa: E402
from errorta_council.coding.ledger import LedgerStore  # noqa: E402
from errorta_council.coding.runner import (  # noqa: E402
    CodingRunner,
    build_run_turn,
    members_by_coding_role,
)

MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]

_GOOD_PY = "def add(a, b):\n    return a + b\n"
# `compileall` is a SYNTAX-level check — this is what it can honestly catch.
_BAD_PY = "def broken(:\n    return\n"


def _task_id(prompt: str, role: str) -> str:
    return re.search(rf"{role} for task id '([^']+)'", prompt).group(1)


def _pr_head(prompt: str) -> str:
    return re.search(r"PR head you are reviewing is '([^']*)'", prompt).group(1)


def _delivery_head(prompt: str) -> str:
    return re.search(r"delivered head you are reviewing is '([^']*)'", prompt).group(1)


def _rev_env(task_id: str, head: str) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "reviewer", "task_id": task_id,
        "intent": {"kind": "review_verdict", "reviewed_head": head,
                   "approved": True, "findings": []}})


class _Fake:
    """One dev task writing `content`, then done. No test commands are ever
    registered — the empty-registry case F154 exists for."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.pm_calls = 0

    def __call__(self, member: dict, prompt: str) -> str:
        if "DELIVERY reviewer" in prompt:
            return _rev_env("delivery-review", _delivery_head(prompt))
        if "You are the PM" in prompt:
            self.pm_calls += 1
            intent = ({"kind": "plan", "done": False,
                       "tasks": [{"title": "implement add", "role": "dev"}]}
                      if self.pm_calls == 1 else
                      {"kind": "plan", "done": True, "completion_summary": "done"})
            return json.dumps({"schema_version": "coding_turn.v1", "role": "pm",
                               "intent": intent})
        if "You are a developer" in prompt:
            return json.dumps({
                "schema_version": "coding_turn.v1", "role": "dev",
                "task_id": _task_id(prompt, "developer"),
                "intent": {"kind": "tool_plan", "task_type": "implementation",
                           "tool_calls": [{"tool": "code_write",
                                           "args": {"path": "calc.py",
                                                    "content": self.content}}]}})
        if "You are a reviewer" in prompt:
            return _rev_env(_task_id(prompt, "reviewer"), _pr_head(prompt))
        if "You are a tester" in prompt:
            return json.dumps({
                "schema_version": "coding_turn.v1", "role": "tester",
                "task_id": _task_id(prompt, "tester"),
                "intent": {"kind": "test_plan", "command_ids": [],
                           "scope": "full_project", "not_applicable": True,
                           "rationale": "no commands registered"}})
        return "{}"


def _run(pid: str, content: str, **policy_kw):
    store = LedgerStore(pid)
    store.create_project(north_star="calc", definition_of_done="add works",
                         target="new", repo_path=None)
    if policy_kw:
        save_policy(store, CodingAutonomyPolicy(**policy_kw))
    runner = CodingRunner(pid, MEMBERS, _Fake(content), guardrail_enabled=True)
    runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF,
                                    max_iterations=40, **policy_kw))
    return store, runner


def _reverify(store, runner):
    """Bust the once-per-head cache and re-run the verifier directly."""
    store.set_run_state(delivery_reviewed_head="__stale__")
    rt = build_run_turn(store, runner.workspace, members_by_coding_role(MEMBERS),
                        lambda m, p: _rev_env("delivery-review",
                                              _delivery_head(p)),
                        guardrail_enabled=True)
    return rt.delivery_review(store)


def test_python_syntax_error_blocks_done(tmp_errorta_home: Path) -> None:
    """The gap F154 closes: an empty registry read as success.

    No test commands are ever registered, so before F154 `tests_passed` was
    unconditionally True and this tree — which does not even parse — reached the
    delivery gate clean.
    """
    store, runner = _run("f154-bad", _BAD_PY)
    result = _reverify(store, runner)
    assert result.passed is False, result
    assert any(t.title == "fix delivery build" for t in store.list_tasks())
    choices = [d.get("choice") for d in store.list_decisions()]
    assert "built_fail" in choices, choices


def test_clean_python_passes(tmp_errorta_home: Path) -> None:
    """The control: a tree that compiles is not blocked by the new gate."""
    store, runner = _run("f154-good", _GOOD_PY)
    result = _reverify(store, runner)
    assert result.passed is True, result
    assert not any(t.title == "fix delivery build" for t in store.list_tasks())


def test_registered_commands_skip_the_default_build(
        tmp_errorta_home: Path) -> None:
    """No behaviour change when the council registered real commands.

    They are the stronger signal; no build is injected on top of them.
    """
    store, runner = _run("f154-registered", _BAD_PY)
    store.set_test_commands({"unit": {
        "argv": [sys.executable, "-c", "pass"], "cwd": ".",
        "timeout_seconds": 30}})
    # Scope to decisions made AFTER registering: the initial run had an empty
    # registry, so it legitimately recorded a build failure of its own.
    before = len(store.list_decisions())
    result = _reverify(store, runner)
    assert result.passed is True, result
    new_choices = [d.get("choice") for d in store.list_decisions()[before:]]
    assert not any(c and c.startswith("built_") for c in new_choices), new_choices
    assert "delivery_build_error" not in new_choices, new_choices


def test_gate_off_restores_todays_behaviour(tmp_errorta_home: Path) -> None:
    """The escape hatch: `default_build_gate=False` reproduces the old pass."""
    store, runner = _run("f154-off", _BAD_PY, default_build_gate=False)
    result = _reverify(store, runner)
    assert result.passed is True, result
    assert not any(t.title == "fix delivery build" for t in store.list_tasks())


def test_no_derivable_build_is_a_noop(tmp_errorta_home: Path) -> None:
    """A stack with no rule derives None and is vacuously clean, as today."""
    store, runner = _run("f154-static", _GOOD_PY)
    # Remove every .py so nothing is derivable from the delivered tree.
    for p in Path(runner.workspace.root()).rglob("*.py"):
        p.unlink()
    (Path(runner.workspace.root()) / "index.html").write_text("<p>hi")
    result = _reverify(store, runner)
    assert result.passed is True, result
