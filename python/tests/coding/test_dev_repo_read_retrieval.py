"""Spec 11 — context self-service (P1a threading + P1b failure propagation).

P1a proves the DEV-turn dispatch threads the task WORKTREE ROOT down to the
gateway member-caller (so a claude_cli dev turn runs read-only in-turn retrieval
with cwd=worktree), gated behind ``CodingAutonomyPolicy.dev_repo_read``. The
provider-side config (read-only tool allowlist, raised max-turns, cwd, envelope
parsing across tool-use turns) is proven in ``tests/test_async_claude_cli.py``.

P1b proves a failing test's verbatim ``stderr_preview`` reaches the filed
fix-task detail, and that the parse-error / unknown-command path is untouched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from errorta_council.coding.autonomy import CADENCE_OFF, CodingAutonomyPolicy
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    CodingRunner,
    _failed_stderr_appendix,
    build_run_turn,
    gateway_member_caller,
    members_by_coding_role,
)
from errorta_council.coding.topology import DEV, Assign
from errorta_council.coding.workspace import CodingWorkspace

MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]


def _real_ws(pid: str, store: LedgerStore) -> CodingWorkspace:
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)
    return ws


def _dev_env(task_id: str) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "dev", "task_id": task_id,
        "intent": {"kind": "tool_plan", "task_type": "implementation",
                   "tool_calls": [{"tool": "code_write",
                                   "args": {"path": "calc.py",
                                            "content": "def add(a,b):\n return a+b\n"}}]}})


# --------------------------------------------------------------------------- #
# P1a — worktree-root threading through the DEV turn.
# --------------------------------------------------------------------------- #

def test_dev_turn_threads_worktree_root_when_enabled(tmp_errorta_home: Path) -> None:
    """dev_repo_read=True => the member handed to the caller on a DEV turn carries
    ``dev_repo_read_root`` = the task worktree root (not a temp dir)."""
    store = LedgerStore("p1a_on")
    store.create_project(north_star="x", definition_of_done="d",
                         target="new", repo_path=None)
    task = store.add_task(title="impl", role=DEV)
    ws = _real_ws("p1a_on", store)
    seen: dict[str, object] = {}

    def caller(member, prompt):
        seen["root"] = member.get("dev_repo_read_root")
        return _dev_env(task.task_id)

    rt = build_run_turn(store, ws, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True, dev_repo_read=True)
    rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)

    root = seen["root"]
    assert isinstance(root, str) and root
    # It is the REAL worktree for this task (an existing dir), never a temp dir.
    assert Path(root).is_dir()
    assert str(ws.task_root(task.task_id)) == root
    # The shared member config was NOT mutated (a per-turn shallow copy).
    assert "dev_repo_read_root" not in MEMBERS[1]


def test_dev_turn_not_tagged_when_disabled(tmp_errorta_home: Path) -> None:
    """dev_repo_read=False (the build_run_turn default) => no worktree root is
    threaded; the legacy single-shot path is preserved."""
    store = LedgerStore("p1a_off")
    store.create_project(north_star="x", definition_of_done="d",
                         target="new", repo_path=None)
    task = store.add_task(title="impl", role=DEV)
    ws = _real_ws("p1a_off", store)
    seen: dict[str, object] = {"root": "UNSET"}

    def caller(member, prompt):
        seen["root"] = member.get("dev_repo_read_root")
        return _dev_env(task.task_id)

    # default dev_repo_read=False
    rt = build_run_turn(store, ws, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)
    assert seen["root"] is None


def test_gateway_caller_forwards_root_to_request_metadata() -> None:
    """The runner->gateway seam: a member tagged with dev_repo_read_root produces
    a LocalCouncilModelRequest whose metadata carries it (which the claude handler
    then reads); an untagged member carries none."""
    captured: dict[str, object] = {}

    class _FakeGateway:
        async def call(self, req):
            captured["metadata"] = dict(req.metadata)

            class _R:
                content = "{}"
                raw_usage_available = False
                input_tokens = None
                output_tokens = None
                provider_class = "claude_cli"
                model = "opus"
                cache_read_input_tokens = None
                cache_write_input_tokens = None
            return _R()

    caller = gateway_member_caller(_FakeGateway())
    caller({"id": "m-dev", "gateway_route_id": "claude_cli.opus",
            "provider_kind": "claude_cli",
            "dev_repo_read_root": "/tmp/wt-xyz"}, "hi")
    assert captured["metadata"].get("dev_repo_read_root") == "/tmp/wt-xyz"

    captured.clear()
    caller({"id": "m-dev", "gateway_route_id": "claude_cli.opus",
            "provider_kind": "claude_cli"}, "hi")
    assert "dev_repo_read_root" not in captured["metadata"]


# --------------------------------------------------------------------------- #
# P1b — verbatim failure propagation.
# --------------------------------------------------------------------------- #

class _FakeResult:
    def __init__(self, command_id, passed, stderr_preview):
        self.command_id = command_id
        self.passed = passed
        self.stderr_preview = stderr_preview


def test_failed_stderr_appendix_includes_only_failing_stderr() -> None:
    results = [
        _FakeResult("unit", True, "ignored (passed)"),
        _FakeResult("integration", False, "AssertionError: expected 3 got 5"),
        _FakeResult("lint", False, ""),  # failed but no stderr -> skipped
    ]
    out = _failed_stderr_appendix(results)
    assert "AssertionError: expected 3 got 5" in out
    assert "[integration]" in out
    assert "ignored (passed)" not in out


def test_failed_stderr_appendix_caps_length() -> None:
    big = "x" * 10000
    out = _failed_stderr_appendix([_FakeResult("unit", False, big)])
    assert 0 < len(out) <= 2000


class _StderrGateway:
    """PM plans one dev task; dev writes calc.py; reviewer approves; tester runs
    the (red, stderr-emitting) unit command."""
    def __init__(self):
        self.pm_calls = 0

    def __call__(self, member, prompt):
        import re
        if "You are the PM" in prompt:
            self.pm_calls += 1
            if self.pm_calls == 1:
                return json.dumps({"schema_version": "coding_turn.v1", "role": "pm",
                                   "intent": {"kind": "plan", "done": False,
                                              "tasks": [{"title": "implement add",
                                                         "role": "dev"}]}})
            return json.dumps({"schema_version": "coding_turn.v1", "role": "pm",
                               "intent": {"kind": "plan", "done": True,
                                          "completion_summary": "done"}})
        if "You are a developer" in prompt:
            tid = re.search(r"task id '([^']+)'", prompt).group(1)
            return _dev_env(tid)
        if "You are a reviewer" in prompt or "DELIVERY reviewer" in prompt:
            tid = re.search(r"task id '([^']+)'", prompt)
            head = re.search(r"head you are reviewing is '([^']*)'", prompt)
            return json.dumps({"schema_version": "coding_turn.v1", "role": "reviewer",
                               "task_id": tid.group(1) if tid else "r",
                               "intent": {"kind": "review_verdict",
                                          "reviewed_head": head.group(1) if head else "",
                                          "approved": True, "findings": []}})
        if "You are a tester" in prompt:
            tid = re.search(r"task id '([^']+)'", prompt).group(1)
            return json.dumps({"schema_version": "coding_turn.v1", "role": "tester",
                               "task_id": tid,
                               "intent": {"kind": "test_plan",
                                          "command_ids": ["unit"], "scope": "full_project",
                                          "rationale": "run"}})
        return "{}"


def test_failing_test_stderr_reaches_fix_task_detail(tmp_errorta_home: Path) -> None:
    """P1b end-to-end: a red test whose stderr contains a distinctive marker ->
    the filed 'fix tests' task detail carries that verbatim stderr, not just
    cmd=status/exit_code."""
    marker = "SENTINEL_STDERR_9f3a: contract mismatch window.AudioModule"
    store = LedgerStore("p1b")
    store.create_project(north_star="x", definition_of_done="d",
                         target="new", repo_path=None)
    store.set_test_commands({"unit": {
        "argv": [sys.executable, "-c",
                 f"import sys; sys.stderr.write({marker!r}); sys.exit(1)"],
        "cwd": ".", "timeout_seconds": 30}})
    runner = CodingRunner("p1b", MEMBERS, _StderrGateway(), guardrail_enabled=True)
    runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_iterations=12))

    fix_tasks = [t for t in store.list_tasks()
                 if t.role == DEV and "fix tests" in t.title]
    assert fix_tasks, "expected a 'fix tests' task to be filed on the red run"
    assert any(marker in (t.detail or "") for t in fix_tasks), \
        [t.detail for t in fix_tasks]


def test_parse_error_path_has_no_stderr_appendix(tmp_errorta_home: Path) -> None:
    """P1b guard: the unknown-command / parse-error fix-task path (runner.py
    ~:3465) must be UNTOUCHED — its detail is the plain 'Make the tests pass:
    <reason>' with no 'Failing test output:' block (no session/stderr in scope)."""
    store = LedgerStore("p1b_parse")
    store.create_project(north_star="x", definition_of_done="d",
                         target="new", repo_path=None)
    store.set_test_commands({"unit": {"argv": [sys.executable, "-c", "pass"],
                                      "cwd": ".", "timeout_seconds": 30}})

    class _BadCmdGateway(_StderrGateway):
        def __call__(self, member, prompt):
            import re
            if "You are a tester" in prompt:
                tid = re.search(r"task id '([^']+)'", prompt).group(1)
                # A command_id that is NOT in the registry -> the fail-closed
                # _changes_requested (parse/unknown) path, no stderr in scope.
                return json.dumps({"schema_version": "coding_turn.v1", "role": "tester",
                                   "task_id": tid,
                                   "intent": {"kind": "test_plan",
                                              "command_ids": ["nonexistent_cmd"],
                                              "scope": "full_project",
                                              "rationale": "run"}})
            return super().__call__(member, prompt)

    runner = CodingRunner("p1b_parse", MEMBERS, _BadCmdGateway(), guardrail_enabled=True)
    runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_iterations=12))

    fix_tasks = [t for t in store.list_tasks()
                 if t.role == DEV and "fix tests" in t.title]
    assert fix_tasks, "expected an unknown-command fix task"
    for t in fix_tasks:
        assert "Make the tests pass:" in (t.detail or "")
        assert "Failing test output:" not in (t.detail or "")
