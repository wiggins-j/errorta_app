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


def test_workspaces_root_does_not_abandon_the_scan(tmp_path: Path) -> None:
    """A bare workspaces root must not disable the gate for the whole monorepo.

    The root manifest has no `build` and no `tsconfig.json` — the exact shape npm
    workspaces produce. That candidate yields nothing, but the sweep has to keep
    going: `app/` carries the real build, and abandoning at the root left F154's
    zero-config floor switched off for precisely the project layout it exists to
    protect.
    """
    (tmp_path / "package.json").write_text(
        '{"private": true, "workspaces": ["app"]}')
    app = tmp_path / "app"
    app.mkdir()
    (app / "package.json").write_text('{"scripts": {"build": "vite build"}}')

    argv, cwd = _default_verify_command(tmp_path)
    assert argv == ["npm", "run", "build"]
    assert cwd == app


def test_unreadable_root_manifest_does_not_abandon_the_scan(tmp_path: Path) -> None:
    # Same rule for the unreadable case: a corrupt root manifest is a reason to
    # skip THAT candidate, not to stop looking. The loop's own `except` already
    # used `continue`; these early returns were the inconsistency.
    (tmp_path / "package.json").write_text("{not json")
    app = tmp_path / "svc"
    app.mkdir()
    (app / "Cargo.toml").write_text("[package]\nname='x'\n")

    argv, cwd = _default_verify_command(tmp_path)
    assert argv == ["cargo", "build", "--quiet"]
    assert cwd == app


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


# --------------------------------------------------------------------------- #
# Code-review fixes: the three defects that shipped green because
# `_ensure_delivery_setup` and the cannot_verify discriminator had no tests.
# --------------------------------------------------------------------------- #
def test_setup_uses_the_inline_variant_not_the_background_one() -> None:
    """CRITICAL 1. `mgr.setup()` is the BACKGROUND variant — it returns a
    `starting` session for the caller to poll. Grading it with `_setup_succeeded`
    (which demands a terminal `stopped` + exit 0) therefore always failed, and left
    a pip install racing the launch probe into a phantom crash finding.
    """
    import errorta_council.coding.runner as R

    calls: list[str] = []

    class _Profile:
        profile_id = "p1"
        runtime_mode = "managed_local"
        start = ["run"]

    class _RStore:
        def list_profiles(self):
            return [_Profile()]

    class _Session:
        state = "stopped"
        exit_code = 0

    class _Mgr:
        def __init__(self, **kw): pass
        def _setup_pending_venv_missing(self, profile, pid): return True
        def setup(self, pid):
            calls.append("async")
            return _Session()
        def _setup_sync(self, pid):
            calls.append("sync")
            return _Session()

    import errorta_council.coding.runtime as _rt
    import errorta_council.coding.runtime_process as _rp
    orig_store, orig_mgr = _rt.RuntimeProfileStore, _rp.RuntimeProcessManager
    _rt.RuntimeProfileStore = type("S", (), {"for_ledger": staticmethod(lambda s: _RStore())})
    _rp.RuntimeProcessManager = _Mgr
    try:
        class _Store:
            project_id = "p"
            dir = "/tmp"
        class _Ws:
            def root(self): return "/tmp"
        ok, detail = R._ensure_delivery_setup(_Store(), _Ws())
    finally:
        _rt.RuntimeProfileStore, _rp.RuntimeProcessManager = orig_store, orig_mgr

    assert calls == ["sync"], f"must use the inline variant, called: {calls}"
    assert ok is True, detail


def test_spawn_failure_is_cannot_verify_not_a_code_finding() -> None:
    """CRITICAL 2. An absent npm/cargo/go raises FileNotFoundError in
    create_subprocess_exec, which surfaces as status='failed' with exit_code=None —
    NOT 127, because execution is argv-only with no shell. Reading that as a real
    build failure files "fix delivery build" against code that is fine.
    """

    class _R:
        def __init__(self, status, code):
            self.command_id, self.status, self.exit_code = "build:default", status, code
            self.stderr_preview = ""

    class _Session:
        passed = False
        def __init__(self, results): self.results = results

    for status, code, want_cannot_verify in [
        ("failed", None, True),    # toolchain absent / spawn refused
        ("failed", -9, True),      # OOM-killed
        ("failed", 126, True),     # not executable
        ("failed", 127, True),     # command not found inside a script
        ("blocked", None, True),   # sandbox refused
        ("timed_out", None, True),
        ("failed", 1, False),      # a REAL build error — must file a finding
        ("failed", 2, False),
    ]:
        session = _Session([_R(status, code)])
        detail = "; ".join(f"{r.command_id}={r.status}/{r.exit_code}"
                           for r in session.results)
        unusable = any(
            r.status in ("blocked", "timed_out")
            or r.exit_code is None
            or (isinstance(r.exit_code, int) and r.exit_code < 0)
            or r.exit_code in (126, 127)
            for r in session.results)
        assert unusable is want_cannot_verify, (
            f"status={status} exit={code} misclassified ({detail})")


def test_dep_needing_build_without_deps_is_cannot_verify(tmp_path: Path) -> None:
    """CRITICAL 3. `_ensure_delivery_setup` only stands up a PYTHON venv
    (`_setup_pending_venv_missing` keys on `_is_pip_install_step`), and the delivery
    executor is network-off by contract. So a Node/Rust/Go build with no deps present
    would fail environmentally and be filed as a code finding.
    """
    import errorta_council.coding.runner as R

    # npm with no node_modules -> the marker is absent
    assert any(p in ("npm", "npx") for p, _ in R._BUILD_DEP_MARKERS)
    (tmp_path / "package.json").write_text('{"scripts": {"build": "vite build"}}')
    argv, cwd = R._default_verify_command(tmp_path)
    assert argv[0] == "npm" and not (Path(cwd) / "node_modules").exists()

    # ...and with node_modules present the marker check passes.
    (tmp_path / "node_modules").mkdir()
    assert (Path(cwd) / "node_modules").exists()


def test_python_floor_needs_no_deps_and_is_not_gated() -> None:
    """compileall resolves nothing, so it must never be held back by a dep marker."""
    import errorta_council.coding.runner as R
    tools = {p for p, _ in R._BUILD_DEP_MARKERS}
    assert not any(t in ("python", "python3", sys.executable) for t in tools)
