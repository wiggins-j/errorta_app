"""Spec 12 (S1) — the acceptance gate runs inside the loop.

The gravity-golf DoD was "iterate until the acceptance gate passes" with NO gate:
the test-command registry is only ever written by the app UI and runtime.detect
is only ever called from an HTTP route, so an autonomous headless run never has
anything to run and every gate is vacuously satisfied. This suite locks the fix
and its two safety properties from the code review:

* D1 — a bootstrapped acceptance command is SMOKE-RUN before it is registered, so
  a command that cannot execute (missing interpreter/dependency) is refused
  instead of becoming a red gate forever.
* the merge gate — an acceptance-scoped command NEVER blocks a per-PR merge (the
  regression that would otherwise wedge every run).
"""
import sys
from pathlib import Path

import pytest

from errorta_council.coding import gate_bootstrap, gate_state, runner
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import GateRun, decide_next, plan_next_batch
from errorta_council.coding.workspace import CodingWorkspace

_PY = sys.executable
_OK = {"argv": [_PY, "-c", "import sys; sys.exit(0)"], "timeout_seconds": 30}
_FAIL = {"argv": [_PY, "-c", "print('boom'); import sys; sys.exit(1)"],
         "timeout_seconds": 30}


def _store(pid: str, tmp_path: Path) -> LedgerStore:
    s = LedgerStore(pid, root=tmp_path / f"ledger-{pid}")
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def _ws(pid: str, store: LedgerStore) -> CodingWorkspace:
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)
    return ws


def _merge_file(ws: CodingWorkspace, task_id: str, path: str, content: str) -> None:
    branch = ws.start_task_branch(task_id)
    ws.write_file(path, content, task_id=task_id)
    assert ws.merge_pr(branch).get("merged")


# --------------------------------------------------------------------------- #
# Phase 1 — acceptance vs unit scope. THE regression lock: an acceptance command
# must not arm the per-PR merge gate.
# --------------------------------------------------------------------------- #


def test_scope_defaults_to_unit_and_filters(tmp_errorta_home: Path,
                                            tmp_path: Path) -> None:
    s = _store("sc1", tmp_path)
    s.set_test_commands({
        "u": {"argv": ["true"], "timeout_seconds": 5},                    # -> unit
        "a": {"argv": ["true"], "timeout_seconds": 5, "scope": "acceptance"}})
    assert s.get_test_commands()["u"]["scope"] == "unit"
    assert set(s.get_unit_test_commands()) == {"u"}
    assert set(s.get_test_commands()) == {"u", "a"}


def test_invalid_scope_is_rejected(tmp_errorta_home: Path, tmp_path: Path) -> None:
    from errorta_council.coding.ledger import LedgerError
    s = _store("sc2", tmp_path)
    try:
        s.set_test_commands({"x": {"argv": ["true"], "scope": "smoke"}})
        raise AssertionError("expected LedgerError")
    except LedgerError:
        pass


def test_acceptance_only_registry_does_not_block_a_merge(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """With ONLY an acceptance command registered, a reviewer-approved PR is still
    mergeable and NO tester task is spawned. Without this, bootstrapping a gate
    would wedge every merge — the batch's load-bearing lock."""
    s = _store("sc3", tmp_path)
    s.set_test_commands({
        "acc": {"argv": ["true"], "timeout_seconds": 5, "scope": "acceptance"}})
    # get_unit_test_commands is empty -> the merge gate is vacuously satisfied,
    # exactly as for a project with no commands at all.
    assert s.get_unit_test_commands() == {}
    # A unit command DOES arm it (unchanged behavior).
    s.set_test_commands({"u": {"argv": ["true"], "timeout_seconds": 5}})
    assert set(s.get_unit_test_commands()) == {"u"}


# --------------------------------------------------------------------------- #
# Phase 2 — the shared deterministic executor.
# --------------------------------------------------------------------------- #


def test_run_gate_returns_none_on_empty_registry(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("rg0", tmp_path)
    ws = _ws("rg0", s)
    assert runner._run_gate(s, ws, head="h", task_id="t") is None


def test_run_gate_runs_all_commands_bound_to_head(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch) -> None:
    s = _store("rg1", tmp_path)
    ws = _ws("rg1", s)
    monkeypatch.setattr(s, "get_require_sandbox", lambda: False)
    s.set_test_commands({
        "u": {**_OK, "scope": "unit"},
        "acc": {**_FAIL, "scope": "acceptance"}})
    session = runner._run_gate(s, ws, head="deadbeef", task_id="in-loop-gate")
    assert session is not None
    # Runs BOTH scopes on the integrated tree.
    assert set(session.command_ids) == {"u", "acc"}
    assert session.passed is False  # the acceptance command failed
    runs = s.list_test_runs()
    assert runs[-1]["head"] == "deadbeef"
    assert runs[-1]["task_id"] == "in-loop-gate"


# --------------------------------------------------------------------------- #
# Phase 3 — gate relevance, arming, scheduling, and the GateRun handler.
# --------------------------------------------------------------------------- #


def test_merge_relevance_ignores_docs_only() -> None:
    assert runner._merge_is_gate_relevant(["src/main.js"]) is True
    assert runner._merge_is_gate_relevant(["test/acceptance.test.js"]) is True
    assert runner._merge_is_gate_relevant(["README.md", "LICENSE"]) is False
    assert runner._merge_is_gate_relevant(["docs/design.md"]) is False
    assert runner._merge_is_gate_relevant([]) is False
    # A mix with any real file is relevant.
    assert runner._merge_is_gate_relevant(["README.md", "src/x.js"]) is True


def test_due_gate_run_reads_the_armed_flag(tmp_errorta_home: Path,
                                           tmp_path: Path) -> None:
    from errorta_council.coding.topology import _due_gate_run
    s = _store("due1", tmp_path)
    assert _due_gate_run(s, "m-pm") is None
    s.set_run_state(gate_due=True)
    got = _due_gate_run(s, "m-pm")
    assert isinstance(got, GateRun) and got.member_id == "m-pm"


def test_scheduler_dispatches_a_due_gate_after_merges_drain(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("sch1", tmp_path)
    s.set_run_state(gate_due=True)
    members = [("m-pm", "pm"), ("m-dev", "dev")]
    # No mergeable PR, gate armed -> decide_next returns a GateRun for the PM.
    action = decide_next(s, members)
    assert isinstance(action, GateRun)
    # The concurrent planner surfaces it too.
    batch = plan_next_batch(s, members)
    assert any(isinstance(a, GateRun) for a in batch)


def test_arm_after_merge_counts_to_the_interval(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch) -> None:
    from errorta_council.coding.autonomy import CodingAutonomyPolicy

    s = _store("arm1", tmp_path)
    ws = _ws("arm1", s)
    monkeypatch.setattr(s, "get_require_sandbox", lambda: False)
    s.set_test_commands({"acc": {**_OK, "scope": "acceptance"}})  # gate_available
    # Force interval=2 via a persisted policy.
    from errorta_council.coding.autonomy import policy_to_dict, save_policy
    save_policy(s, CodingAutonomyPolicy(gate_min_merge_interval=2))

    runner._arm_gate_after_merge(s, ws, changed=["src/a.js"], head="h1")
    assert s.get_run_state().get("gate_due") in (None, False)
    assert int(s.get_run_state().get("gate_pending_merges", 0)) == 1
    runner._arm_gate_after_merge(s, ws, changed=["src/b.js"], head="h2")
    assert s.get_run_state().get("gate_due") is True
    assert s.get_run_state().get("gate_dirty_head") == "h2"
    # The counter reset on arming.
    assert int(s.get_run_state().get("gate_pending_merges", 0)) == 0
    _ = policy_to_dict  # keep the import meaningful


def test_arm_after_merge_ignores_docs_only_merge(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch) -> None:
    s = _store("arm2", tmp_path)
    ws = _ws("arm2", s)
    monkeypatch.setattr(s, "get_require_sandbox", lambda: False)
    s.set_test_commands({"acc": {**_OK, "scope": "acceptance"}})
    runner._arm_gate_after_merge(s, ws, changed=["README.md"], head="h1")
    assert int(s.get_run_state().get("gate_pending_merges", 0)) == 0


def test_arm_after_merge_noop_without_a_gate(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("arm3", tmp_path)
    ws = _ws("arm3", s)
    # No commands, no runtime profile -> nothing to run -> never arms.
    runner._arm_gate_after_merge(s, ws, changed=["src/a.js"], head="h1")
    assert int(s.get_run_state().get("gate_pending_merges", 0)) == 0
    assert s.get_run_state().get("gate_due") in (None, False)


# --------------------------------------------------------------------------- #
# Phase 4 — bootstrap: detection, the smoke-run safeguard (D1), idempotence.
# --------------------------------------------------------------------------- #


def test_detect_acceptance_command_prefers_named_js_test() -> None:
    got = gate_bootstrap._detect_acceptance_command(
        ["index.html", "test/acceptance.test.js", "test/util.test.js"])
    assert got is not None
    cmd_id, spec = got
    assert spec["argv"] == ["node", "test/acceptance.test.js"]
    assert spec["scope"] == "acceptance"


def test_detect_acceptance_command_falls_back_to_pytest() -> None:
    got = gate_bootstrap._detect_acceptance_command(["tests/test_app.py", "app.py"])
    assert got is not None
    _, spec = got
    assert spec["argv"][:3] == ["python", "-m", "pytest"]


def test_detect_acceptance_command_none_without_tests() -> None:
    assert gate_bootstrap._detect_acceptance_command(["index.html", "app.js"]) is None


def test_smoke_ran_cleanly_distinguishes_ran_from_unrunnable() -> None:
    from errorta_council.coding.testing import TestRunResult, TestRunSession

    def _sess(status, stderr="", exit_code=0):
        return TestRunSession(
            command_ids=["acc"], unknown_ids=[], passed=(exit_code == 0),
            results=[TestRunResult(
                command_id="acc", argv_sha256="a" * 64, status=status,
                exit_code=exit_code, passed=(exit_code == 0), duration_ms=1,
                stdout_sha256="b" * 64, stdout_preview="", stderr_preview=stderr)])

    # A real test failure (ran, non-zero) IS registrable — that's the signal.
    assert gate_bootstrap._smoke_ran_cleanly(_sess("completed", exit_code=1))[0] is True
    # A clean pass, obviously.
    assert gate_bootstrap._smoke_ran_cleanly(_sess("completed", exit_code=0))[0] is True
    # A missing dependency (node/jsdom) is NOT registrable — the D1 wedge.
    assert gate_bootstrap._smoke_ran_cleanly(
        _sess("completed", stderr="Error: Cannot find module 'jsdom'", exit_code=1))[0] is False
    # A missing interpreter / failed launch.
    assert gate_bootstrap._smoke_ran_cleanly(_sess("blocked"))[0] is False
    assert gate_bootstrap._smoke_ran_cleanly(
        _sess("completed", stderr="python: command not found"))[0] is False


@pytest.mark.parametrize("stderr, stdout, expected_ran, note", [
    # --- launch failures: the interpreter/entrypoint never ran a test → refuse.
    ("node:internal/modules/cjs/loader ... code: 'ERR_MODULE_NOT_FOUND'", "",
     False, "node loader failed before user code"),
    ("ModuleNotFoundError: No module named 'pytest'", "",
     False, "python import failed at collection (no test ran)"),
    ("Error: Cannot find module 'jsdom'", "",
     False, "node missing dependency, no test framework output"),
    ("node acceptance.test.js: No such file or directory", "",
     False, "entrypoint absent, no test framework output"),
    ("/bin/sh: pytest: command not found", "",
     False, "interpreter absent from PATH"),
    # --- real tests that RAN and asserted on a missing file/module → register.
    #     Each carries genuine test-framework output plus the ambiguous phrase.
    ("AssertionError: expected level file to exist",
     "1 passed, 1 failed in 0.03s\nE  FileNotFoundError: [Errno 2] "
     "No such file or directory: 'levels/1.json'",
     True, "pytest RAN, failed a file-I/O assertion"),
    ("Error: Cannot find module '../levels/boss'",
     "  gravity-golf\n    1 passing\n    1 failing\n"
     "  ✗ loads the boss level",
     True, "mocha RAN, failed a module-resolution assertion"),
    ("",
     "TAP version 13\nok 1 - boots\nnot ok 2 - opens save file: "
     "ENOENT: no such file or directory",
     True, "node:test/TAP RAN, failed on a missing file"),
])
def test_smoke_ran_cleanly_ambiguous_signatures_need_a_launch_signal(
        stderr, stdout, expected_ran, note) -> None:
    """The ambiguous phrases ("no such file or directory", "cannot find module",
    "no module named") only refuse when NO test framework ran. A real test that
    executed and asserted on a missing file/module is registered — the D1 signal
    we want — while a genuine failed launch is still refused."""
    from errorta_council.coding.testing import TestRunResult, TestRunSession

    session = TestRunSession(
        command_ids=["acc"], unknown_ids=[], passed=False,
        results=[TestRunResult(
            command_id="acc", argv_sha256="a" * 64, status="completed",
            exit_code=1, passed=False, duration_ms=1, stdout_sha256="b" * 64,
            stdout_preview=stdout, stderr_preview=stderr)])
    ran, reason = gate_bootstrap._smoke_ran_cleanly(session)
    assert ran is expected_ran, f"{note}: {reason}"


def test_maybe_bootstrap_registers_a_runnable_command(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch) -> None:
    """End-to-end command step with detection monkeypatched to a trivially-runnable
    argv, so the test doesn't depend on node/pytest in the sandbox."""
    from errorta_council.coding.autonomy import CodingAutonomyPolicy

    s = _store("bs1", tmp_path)
    ws = _ws("bs1", s)
    monkeypatch.setattr(s, "get_require_sandbox", lambda: False)
    # SPEC-34 S2: a GREEN smoke must PROVE it asserted something, so the stub emits
    # a pass line (a real passing acceptance test always reports its count).
    monkeypatch.setattr(
        gate_bootstrap, "_detect_acceptance_command",
        lambda files, **_kw: ("acceptance", {
            "argv": [_PY, "-c", "print('1 passed')"], "timeout_seconds": 30,
            "scope": "acceptance", "cwd": ".", "label": "acc"}))
    gate_bootstrap.maybe_bootstrap(s, ws, CodingAutonomyPolicy())
    assert "acceptance" in s.get_test_commands()
    assert s.get_test_commands()["acceptance"]["scope"] == "acceptance"
    assert any(d["choice"] == "gate_bootstrapped" for d in s.list_decisions())


def test_maybe_bootstrap_refuses_an_unrunnable_command(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch) -> None:
    from errorta_council.coding.autonomy import CodingAutonomyPolicy

    s = _store("bs2", tmp_path)
    ws = _ws("bs2", s)
    monkeypatch.setattr(s, "get_require_sandbox", lambda: False)
    monkeypatch.setattr(
        gate_bootstrap, "_detect_acceptance_command",
        lambda files, **_kw: ("acceptance", {
            "argv": ["this-binary-does-not-exist-spec12", "x"],
            "timeout_seconds": 5, "cwd": ".", "scope": "acceptance",
            "label": "acc"}))
    gate_bootstrap.maybe_bootstrap(s, ws, CodingAutonomyPolicy())
    assert s.get_test_commands() == {}  # NOT registered
    # SPEC-31: an un-runnable authored test is recorded as test_runtime_unavailable
    # (still not registered as a gate) rather than silently refused.
    assert any(d["choice"] == "test_runtime_unavailable" for d in s.list_decisions())
    assert s.get_run_state().get("acceptance_test_unrun")  # persisted for the done ack


def test_a_refused_candidate_smoke_runs_at_most_once(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch) -> None:
    """Regression lock: a refused candidate registers nothing, so without the
    run-state memo the smoke `run_test_commands` would re-execute on every merge
    (a repeated subprocess on the merge turn + a `gate_bootstrap_refused` each
    time). Assert it runs exactly once across repeated bootstrap calls."""
    from errorta_council.coding import testing as _testing
    from errorta_council.coding.autonomy import CodingAutonomyPolicy

    s = _store("bsmemo", tmp_path)
    ws = _ws("bsmemo", s)
    monkeypatch.setattr(s, "get_require_sandbox", lambda: False)
    monkeypatch.setattr(
        gate_bootstrap, "_detect_acceptance_command",
        lambda files, **_kw: ("acceptance", {"argv": ["x"], "timeout_seconds": 5,
                                      "cwd": ".", "scope": "acceptance",
                                      "label": "acc"}))
    calls = {"n": 0}

    class _EmptySession:
        results: list = []

    def _fake_run(*_a, **_k):
        calls["n"] += 1
        return _EmptySession()  # no results -> _smoke_ran_cleanly refuses

    monkeypatch.setattr(_testing, "run_test_commands", _fake_run)
    for _ in range(3):
        gate_bootstrap.maybe_bootstrap(s, ws, CodingAutonomyPolicy())

    assert calls["n"] == 1  # memoized: smoke ran once, not once per merge
    assert s.get_test_commands() == {}  # still refused, never registered
    assert s.get_run_state().get("gate_cmd_bootstrap_resolved") is True


def test_maybe_bootstrap_never_overwrites_an_operator_registry(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    from errorta_council.coding.autonomy import CodingAutonomyPolicy
    s = _store("bs3", tmp_path)
    ws = _ws("bs3", s)
    s.set_test_commands({"mine": {"argv": ["true"], "timeout_seconds": 5}})
    gate_bootstrap.maybe_bootstrap(s, ws, CodingAutonomyPolicy())
    assert set(s.get_test_commands()) == {"mine"}


def test_maybe_bootstrap_registers_runtime_profiles_for_a_web_tree(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    from errorta_council.coding.autonomy import CodingAutonomyPolicy
    from errorta_council.coding.runtime import RuntimeProfileStore

    s = _store("bs4", tmp_path)
    ws = _ws("bs4", s)
    _merge_file(ws, "t1", "index.html", "<html><body>hi</body></html>")
    gate_bootstrap.maybe_bootstrap(s, ws, CodingAutonomyPolicy())
    profiles = RuntimeProfileStore.for_ledger(s).list_profiles()
    # A static index.html tree detects a served-static runtime.
    assert any(getattr(p, "kind", "") == "static" for p in profiles)


def test_maybe_bootstrap_respects_the_policy_switch(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch) -> None:
    from errorta_council.coding.autonomy import CodingAutonomyPolicy
    s = _store("bs5", tmp_path)
    ws = _ws("bs5", s)
    called = {"n": 0}
    monkeypatch.setattr(gate_bootstrap, "_bootstrap_runtime",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    gate_bootstrap.maybe_bootstrap(s, ws, CodingAutonomyPolicy(gate_bootstrap=False))
    assert called["n"] == 0


# --------------------------------------------------------------------------- #
# Phase 6 — verbatim gate output reaches the prompts (absent when no run).
# --------------------------------------------------------------------------- #


def test_dev_prompt_carries_gate_output_when_a_run_exists(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch) -> None:
    s = _store("po1", tmp_path)
    ws = _ws("po1", s)
    monkeypatch.setattr(s, "get_require_sandbox", lambda: False)
    s.set_test_commands({"acc": {**_FAIL, "scope": "acceptance"}})
    runner._run_gate(s, ws, head="cafef00d", task_id="in-loop-gate")

    task = s.add_task(title="fix it", role="dev", detail="make the gate pass")
    prompt = runner._dev_prompt(task, s, readback="x = 1\n")
    assert "acceptance gate" in prompt.lower()
    assert "cafef00d"[:12] in prompt


def test_dev_prompt_has_no_gate_segment_without_a_run(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("po2", tmp_path)
    task = s.add_task(title="build", role="dev", detail="do the thing")
    prompt = runner._dev_prompt(task, s, readback="")
    assert gate_state.latest_gate_text(s) == ""
    # Spec 17: the tool_guidance now always names the acceptance gate as the source
    # of execution evidence, so "acceptance gate" is no longer a proxy for the
    # gate-OUTPUT segment. The gate-RUN block (emitted only after a run) is the real
    # marker of that segment — assert IT is absent when nothing has run.
    assert "latest acceptance gate run" not in prompt.lower()


# --------------------------------------------------------------------------- #
# Phase 7 — completion is bound to a green gate at the delivered head. No new
# predicate: delivery_review already runs the WHOLE registry against the merged
# head via the shared _run_gate; a bootstrapped acceptance command flows through
# it. This asserts the shared executor is the one delivery uses.
# --------------------------------------------------------------------------- #


def test_delivery_uses_the_shared_run_gate(monkeypatch) -> None:
    """delivery_review's test step calls _run_gate (Phase 2 extraction), so a
    bootstrapped acceptance command is verified on the delivered head and a red
    result blocks `passed` — the completion guarantee, via existing machinery."""
    import inspect

    from errorta_council.coding import runner as r
    src = inspect.getsource(r.build_run_turn)
    # The delivery_review path invokes the shared executor rather than a second
    # inlined run_test_commands call.
    assert "_run_gate(store, workspace, head=head" in src


# --------------------------------------------------------------------------- #
# Phase 8 (Item 5) — the runtime-probe arm. A runtime-ONLY project (a runnable
# managed_local profile, NO test commands — the buildless-web case) gets a real
# in-loop validation arm: `_run_gate(probe_runtime=True)` drives the F146 Slice C
# launch machinery and folds a `runtime:launch` verdict into the gate record, so a
# crash-on-start is a red gate in-loop instead of only at delivery. These spawn
# REAL short-lived children through the F039 sandbox (as the F146 suite does).
# --------------------------------------------------------------------------- #

_CLEAN_START = ["python", "-c", "print('booted ok')"]
_CRASH_START = ["python", "-c", "raise RuntimeError('startup boom: import failed')"]


@pytest.fixture
def _fast_probe(monkeypatch):
    """Keep the launch probe fast and never leak a child across tests."""
    from errorta_council.coding import runtime_process as rp
    monkeypatch.setattr(rp, "_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(rp, "_GRACE_SECONDS", 1.0)
    monkeypatch.setattr(rp, "_LAUNCH_PROBE_SECONDS", 1.0)
    monkeypatch.setattr(rp, "_LAUNCH_HTTP_PROBE_SECONDS", 2.5)
    yield
    rp.teardown_all()


def _add_runtime(store: LedgerStore, start_argv, *, kind: str = "cli") -> None:
    from errorta_council.coding.runtime import RuntimeProfileStore, validate_profile
    rstore = RuntimeProfileStore.for_ledger(store)
    rstore.upsert_profile(validate_profile(
        {"kind": kind, "runtime_mode": "managed_local", "start": start_argv,
         "sandbox": "auto"}, profile_id="default", project_id=store.project_id))


def test_run_gate_runtime_only_produces_a_gate_record(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch, _fast_probe) -> None:
    """Item 5's acceptance: a project with a runnable profile and NO commands
    produces an in-loop gate record from the runtime probe."""
    s = _store("rt1", tmp_path)
    ws = _ws("rt1", s)
    monkeypatch.setattr(s, "get_require_sandbox", lambda: False)
    _add_runtime(s, _CLEAN_START)
    assert s.get_test_commands() == {}  # runtime-only: the gate has no commands

    session = runner._run_gate(s, ws, head="feedface", task_id="in-loop-gate",
                               probe_runtime=True)
    assert session is not None
    assert "runtime:launch" in session.command_ids
    assert session.passed is True  # a clean launch is a green gate
    runs = s.list_test_runs()
    assert runs[-1]["head"] == "feedface"
    assert any(r["command_id"] == "runtime:launch" for r in runs[-1]["results"])


def test_run_gate_runtime_crash_is_a_red_gate(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch, _fast_probe) -> None:
    """A crash-on-start is caught in-loop as a red gate — the whole point of the
    arm (delivery no longer the first place a startup crash surfaces)."""
    s = _store("rt2", tmp_path)
    ws = _ws("rt2", s)
    monkeypatch.setattr(s, "get_require_sandbox", lambda: False)
    _add_runtime(s, _CRASH_START, kind="desktop")

    session = runner._run_gate(s, ws, head="deadc0de", task_id="in-loop-gate",
                               probe_runtime=True)
    assert session is not None
    assert session.passed is False
    rt = [r for r in session.results if r.command_id == "runtime:launch"]
    assert rt and rt[0].passed is False


def test_run_gate_folds_runtime_probe_alongside_commands(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch, _fast_probe) -> None:
    """Commands + runtime fold into ONE recorded session (a single latest gate
    record for Spec 04 + the prompt segments), not two."""
    s = _store("rt3", tmp_path)
    ws = _ws("rt3", s)
    monkeypatch.setattr(s, "get_require_sandbox", lambda: False)
    s.set_test_commands({"acc": {**_OK, "scope": "acceptance"}})
    _add_runtime(s, _CLEAN_START)

    before = len(s.list_test_runs())
    session = runner._run_gate(s, ws, head="cafebabe", task_id="in-loop-gate",
                               probe_runtime=True)
    assert session is not None
    assert set(session.command_ids) == {"acc", "runtime:launch"}
    assert session.passed is True
    # Exactly one new gate record carrying both verdicts.
    assert len(s.list_test_runs()) == before + 1


def test_runtime_arm_is_opt_in_and_leaves_tester_condition_alone(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch, _fast_probe) -> None:
    """Regression lock (Δ review): the runtime arm lives in the in-loop gate, not
    the TESTER branch. The default `_run_gate` (probe_runtime=False — delivery's
    own call) never probes runtime, and the tester-spawn condition keys on UNIT
    commands only, so a runnable-profile-only project spawns no tester turn."""
    s = _store("rt4", tmp_path)
    ws = _ws("rt4", s)
    monkeypatch.setattr(s, "get_require_sandbox", lambda: False)
    _add_runtime(s, _CLEAN_START)
    # Opt-in: without probe_runtime a runtime-only project still returns None.
    assert runner._run_gate(s, ws, head="h", task_id="t") is None
    # The tester-spawn condition is unchanged — no unit command exists.
    assert s.get_unit_test_commands() == {}


def test_tester_spawn_condition_and_not_applicable_path_unchanged() -> None:
    """Source lock for Item 5's regression: the runtime arm must NOT relax the
    tester-spawn condition (it still gates on unit commands) and the
    `not_applicable` escape must remain intact.

    SPEC-26 (S4) TIGHTENED the condition — it now also requires a seated TESTER, so
    unseating one cannot leave an approved PR waiting on a task nobody can take. The
    unit-command half this test exists to lock is unchanged."""
    import inspect

    from errorta_council.coding import runner as r
    src = inspect.getsource(r.build_run_turn)
    # tester spawn still gates on unit commands (SPEC-26 adds a conjunct, never
    # relaxes this one)
    assert "if store.get_unit_test_commands() and _tester_seated():" in src
    assert "tests_not_applicable" in src                # not_applicable path intact


def test_arm_after_merge_arms_on_a_runtime_only_project(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch) -> None:
    """The stopgap is reverted: a runtime-only project (a runnable profile, no
    commands) now arms gate_due once the interval accumulates — because the
    runtime arm gives the GateRun something to execute."""
    from errorta_council.coding.autonomy import CodingAutonomyPolicy, save_policy

    s = _store("rt5", tmp_path)
    ws = _ws("rt5", s)
    _add_runtime(s, _CLEAN_START)
    save_policy(s, CodingAutonomyPolicy(gate_min_merge_interval=1))
    runner._arm_gate_after_merge(s, ws, changed=["src/a.py"], head="h1")
    assert s.get_run_state().get("gate_due") is True


def test_runtime_probe_exception_degrades_cleanly(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch) -> None:
    """A runtime-probe exception records a decision and never fails the turn (the
    gate path is fail-open). No process spawn — the launch helper is stubbed."""
    s = _store("rt6", tmp_path)
    ws = _ws("rt6", s)
    _add_runtime(s, _CLEAN_START)

    def _boom(*a, **k):
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(runner, "_delivery_launch_evidence", _boom)

    session = runner._run_gate(s, ws, head="h", task_id="in-loop-gate",
                               probe_runtime=True)
    assert session is None  # no command session, probe raised -> nothing recorded
    assert [c for c in (d.get("choice") for d in s.list_decisions())
            if c == "gate_runtime_error"]


def test_runtime_cannot_verify_is_not_a_red_gate(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch) -> None:
    """An INABILITY to launch (sandbox/setup/spawn — cannot_verify) is
    environmental, not a code crash: it records a decision, NOT a red gate that
    would churn Spec 04's stall detector."""
    s = _store("rt7", tmp_path)
    ws = _ws("rt7", s)
    _add_runtime(s, _CLEAN_START)
    monkeypatch.setattr(runner, "_delivery_launch_evidence",
                        lambda *a, **k: (False, True, "sandbox unavailable"))

    session = runner._run_gate(s, ws, head="h", task_id="in-loop-gate",
                               probe_runtime=True)
    assert session is None
    assert s.list_test_runs() == []  # no red gate record
    assert "gate_runtime_cannot_verify" in [d.get("choice") for d in s.list_decisions()]
