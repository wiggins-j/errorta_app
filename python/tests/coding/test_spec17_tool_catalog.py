"""Spec 17 — prompt / tool-catalog coherence.

The work-request framing, the tool catalog, and the real CLI-native tool surface
must describe the SAME reality, derived from one source. These tests lock:

* Item 1 — the capability-aware ``tool_catalog_text`` (table-driven over role ×
  repo_read × gate): the errorta-tool-list invariant, the no-execute-tool
  sentence in every variant, required keyword args, and derivation from
  ``_ROLE_TOOLS``.
* Item 2 — the ``dev_repo_read`` drift lock (field default == documented default;
  ``reviewer_repo_read`` agrees).
* Item 3 — the ``tool_not_allowed`` corrective hint names the real tools AND is
  carried forward on the task's next composed prompt, cleared after a write, and
  still leaves the turn unproductive when nothing was written (F136 lock).
"""
from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from errorta_council.coding import turn_controller
from errorta_council.coding.autonomy import CodingAutonomyPolicy
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import DEV, PM, REVIEWER, TESTER
from errorta_council.coding.turn_controller import (
    CodingTurnController,
    allowed_tools_for_role,
    tool_catalog_text,
)
from errorta_council.coding.workspace import CodingWorkspace

_ROLES = (PM, DEV, REVIEWER, TESTER)
_NO_EXEC_SENTENCE = "There is no execute, run, or shell tool for any role"


# --------------------------------------------------------------------------- #
# Item 1 — capability-aware tool_catalog_text (role × repo_read × gate).
# --------------------------------------------------------------------------- #

def _variants():
    for role, repo_read, gate in itertools.product(
            _ROLES, (False, True), (False, True)):
        yield role, repo_read, gate


def test_every_variant_states_no_execute_tool() -> None:
    for role, repo_read, gate in _variants():
        txt = tool_catalog_text(role, repo_read=repo_read, gate=gate)
        assert _NO_EXEC_SENTENCE in txt, (role, repo_read, gate)


def test_errorta_tool_list_invariant_holds_in_every_variant() -> None:
    """The F087-14 WS-3 discipline: the errorta-tool list embedded in ANY
    rendering equals ``", ".join(allowed_tools_for_role(role)) or "none"`` —
    nothing advertised that is not executed."""
    for role, repo_read, gate in _variants():
        expected = ", ".join(allowed_tools_for_role(role)) or "none"
        txt = tool_catalog_text(role, repo_read=repo_read, gate=gate)
        assert f"role {role}: {expected}." in txt, (role, repo_read, gate)


def test_repo_read_on_names_native_read_tools() -> None:
    for role in _ROLES:
        txt = tool_catalog_text(role, repo_read=True, gate=False)
        assert "Read, Grep, and Glob" in txt
        # Used directly, NOT emitted as errorta tool calls.
        assert "do NOT emit them as Coding Mode tool calls" in txt
        assert "context_request" not in txt


def test_repo_read_off_dev_names_context_request() -> None:
    txt = tool_catalog_text(DEV, repo_read=False, gate=False)
    assert "context_request" in txt
    assert "Read, Grep, and Glob" not in txt


def test_repo_read_off_non_dev_has_no_read_tool_and_no_context_request() -> None:
    # context_request is a DEV intent today; reviewer/tester without repo_read
    # judge from the context already provided, not a typed ask they cannot emit.
    for role in (PM, REVIEWER, TESTER):
        txt = tool_catalog_text(role, repo_read=False, gate=False)
        assert "context_request" not in txt
        assert "no in-turn file-read tool" in txt


def test_gate_flag_changes_the_negative_sentence() -> None:
    with_gate = tool_catalog_text(DEV, repo_read=False, gate=True)
    without = tool_catalog_text(DEV, repo_read=False, gate=False)
    assert "output appears in the gate section above" in with_gate
    assert "none is configured for this run yet" in without
    assert with_gate != without


def test_keyword_args_are_required() -> None:
    with pytest.raises(TypeError):
        tool_catalog_text(DEV)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tool_catalog_text(DEV, repo_read=True)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tool_catalog_text(DEV, gate=True)  # type: ignore[call-arg]


def test_adding_a_tool_to_role_tools_changes_every_rendering(monkeypatch) -> None:
    """Derivation, not authorship: a new tool in ``_ROLE_TOOLS`` shows up in every
    rendering for that role with NO edit to ``tool_catalog_text``."""
    patched = dict(turn_controller._ROLE_TOOLS)
    patched[REVIEWER] = ("code_read",)
    monkeypatch.setattr(turn_controller, "_ROLE_TOOLS", patched)
    for repo_read, gate in itertools.product((False, True), (False, True)):
        txt = tool_catalog_text(REVIEWER, repo_read=repo_read, gate=gate)
        # The invariant follows the patched table with no function edit.
        assert "role reviewer: code_read." in txt


# --------------------------------------------------------------------------- #
# Item 2 — the dev_repo_read drift lock (field default is the single source).
# --------------------------------------------------------------------------- #

def test_repo_read_defaults_agree_and_are_false() -> None:
    base = CodingAutonomyPolicy()
    assert base.dev_repo_read is False
    assert base.reviewer_repo_read == base.dev_repo_read


def test_autonomy_prose_does_not_claim_a_different_default() -> None:
    import inspect

    from errorta_council.coding import autonomy

    src = inspect.getsource(autonomy)
    field_default = CodingAutonomyPolicy().dev_repo_read
    claims_on = ("Default ON" in src) or ("dataclass default (True)" in src)
    assert claims_on == bool(field_default)


# --------------------------------------------------------------------------- #
# Item 1 (follow-up) — vendor-honor repo_read so the prompt can't lie.
#
# The dev/reviewer/pm-review dispatch tags a per-turn member copy with
# ``repo_read_root`` ONLY when the member's vendor actually runs the read-only
# cwd invocation (claude_cli today). A codex/cursor member with the policy ON
# must get the repo_read=OFF catalog (no Read/Grep/Glob named) and must NOT have
# the no-op ``repo_read_root`` metadata forwarded.
# --------------------------------------------------------------------------- #

def _dev_env(task_id: str) -> str:
    import json
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "dev", "task_id": task_id,
        "intent": {"kind": "tool_plan", "task_type": "implementation",
                   "tool_calls": [{"tool": "code_write",
                                   "args": {"path": "calc.py",
                                            "content": "def add(a,b):\n return a+b\n"}}]}})


def _dev_members(vendor_route: str | None):
    m = {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}}
    if vendor_route is not None:
        m["provider_kind"] = vendor_route.split(".", 1)[0]
        m["gateway_route_id"] = vendor_route
    return [m]


def _run_dev_turn_capture_prompt(vendor_route: str | None, tmp_home: Path):
    """Drive one dev turn with dev_repo_read ON; return the prompt the member
    received (so we can assert what the tool catalog told it)."""
    from errorta_council.coding.runner import (
        build_run_turn,
        members_by_coding_role,
    )
    from errorta_council.coding.topology import Assign

    pid = "s17vh_" + (vendor_route or "none").replace(".", "_")
    store = LedgerStore(pid)
    store.create_project(north_star="x", definition_of_done="d",
                         target="new", repo_path=None)
    task = store.add_task(title="impl", role=DEV)
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)

    seen: dict[str, object] = {}

    def caller(member, prompt):
        seen["prompt"] = prompt
        seen["root"] = member.get("repo_read_root")
        return _dev_env(task.task_id)

    rt = build_run_turn(store, ws, members_by_coding_role(_dev_members(vendor_route)),
                        caller, guardrail_enabled=True, dev_repo_read=True)
    rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)
    return seen


def test_non_claude_cli_dev_gets_repo_read_off_catalog(tmp_errorta_home: Path) -> None:
    """The core fix: a codex dev with dev_repo_read ON is NOT tagged, so its
    prompt shows the repo_read=OFF (context_request) variant — no Read/Grep/Glob
    named, and no worktree root threaded."""
    seen = _run_dev_turn_capture_prompt("codex_cli.gpt5", tmp_errorta_home)
    prompt = str(seen["prompt"])
    assert "context_request" in prompt
    assert "Read, Grep, and Glob" not in prompt
    assert seen["root"] is None


def test_claude_cli_dev_still_gets_repo_read_on(tmp_errorta_home: Path) -> None:
    """Regression lock: a claude_cli dev with the flag on keeps repo_read ON —
    the catalog names the native read tools and the worktree root is threaded."""
    seen = _run_dev_turn_capture_prompt("claude_cli.opus", tmp_errorta_home)
    prompt = str(seen["prompt"])
    assert "Read, Grep, and Glob" in prompt
    assert "context_request" not in prompt
    assert isinstance(seen["root"], str) and seen["root"]


def test_metadata_not_forwarded_for_non_honoring_vendor() -> None:
    """The gateway seam vendor-gates the forwarding too: even if a non-honoring
    member somehow carries ``repo_read_root``, the request metadata drops it (so
    the prompt catalog and the forwarded key never disagree)."""
    from errorta_council.coding.runner import gateway_member_caller

    captured: dict[str, object] = {}

    class _FakeGateway:
        async def call(self, req):
            captured["metadata"] = dict(req.metadata)

            class _R:
                content = "{}"
                raw_usage_available = False
                input_tokens = None
                output_tokens = None
                provider_class = "codex_cli"
                model = "gpt5"
                cache_read_input_tokens = None
                cache_write_input_tokens = None
            return _R()

    caller = gateway_member_caller(_FakeGateway())
    caller({"id": "m-dev", "gateway_route_id": "codex_cli.gpt5",
            "provider_kind": "codex_cli",
            "repo_read_root": "/tmp/wt-xyz"}, "hi")
    assert "repo_read_root" not in captured["metadata"]

    # Honoring vendor still forwards it (paired regression).
    captured.clear()
    caller({"id": "m-dev", "gateway_route_id": "claude_cli.opus",
            "provider_kind": "claude_cli",
            "repo_read_root": "/tmp/wt-xyz"}, "hi")
    assert captured["metadata"].get("repo_read_root") == "/tmp/wt-xyz"


def test_member_honors_repo_read_helper() -> None:
    """The centralized predicate: ON only when BOTH the policy flag and a
    honoring vendor; a future vendor is a one-line add to the vendor set."""
    from errorta_council.coding.runner import (
        _REPO_READ_HONORING_VENDORS,
        _member_honors_repo_read,
    )

    claude = {"gateway_route_id": "claude_cli.opus", "provider_kind": "claude_cli"}
    codex = {"gateway_route_id": "codex_cli.gpt5", "provider_kind": "codex_cli"}
    assert _member_honors_repo_read(claude, True) is True
    assert _member_honors_repo_read(claude, False) is False   # policy gates too
    assert _member_honors_repo_read(codex, True) is False      # vendor gates
    # provider_kind fallback when the route carries no prefix.
    assert _member_honors_repo_read({"provider_kind": "claude_cli"}, True) is True
    assert _REPO_READ_HONORING_VENDORS == frozenset({"claude_cli"})


# --------------------------------------------------------------------------- #
# Item 3 — the tool_not_allowed corrective hint + carry-forward.
# --------------------------------------------------------------------------- #

def _store(project_id: str) -> LedgerStore:
    s = LedgerStore(project_id)
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def _workspace(project_id: str, store: LedgerStore) -> CodingWorkspace:
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    return ws


def test_unknown_tool_error_names_the_real_tools(tmp_errorta_home: Path) -> None:
    store = _store("s17err")
    task = store.add_task(title="impl", role="dev")
    ws = _workspace("s17err", store)
    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=task, member={"id": "m-dev"},
        data={"tool_calls": [{"tool": "read_files", "args": {"path": "x"}}]},
    )
    assert summary.success_count == 0
    (bad_tool, reason), = summary.failures
    assert bad_tool == "read_files"
    assert reason.startswith("tool_not_allowed")
    assert "read_files" in reason
    assert "code_write" in reason           # names the allowed tool
    assert "context_request" in reason      # names the real read path (no repo_read)


def test_unknown_tool_error_names_read_grep_glob_when_repo_read(
        tmp_errorta_home: Path) -> None:
    store = _store("s17err2")
    task = store.add_task(title="impl", role="dev")
    ws = _workspace("s17err2", store)
    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=task, member={"id": "m-dev", "repo_read_root": "/tmp/wt"},
        data={"tool_calls": [{"tool": "read_files", "args": {"path": "x"}}]},
    )
    (_, reason), = summary.failures
    assert "Read/Grep/Glob" in reason
    assert "context_request" not in reason


def test_next_dev_prompt_carries_the_tool_failure(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.runner import _dev_prompt

    store = _store("s17carry")
    task = store.add_task(
        title="impl", role="dev",
        last_tool_failure="tool_not_allowed: 'read_files' — this role executes "
        "only: code_write; to read a file use a context_request intent")
    prompt = _dev_prompt(task, store)
    assert "read_files" in prompt
    assert "tool call rejected" in prompt


def test_tool_failure_clears_from_dev_prompt_when_absent(
        tmp_errorta_home: Path) -> None:
    from errorta_council.coding.runner import _dev_prompt

    store = _store("s17clear")
    task = store.add_task(title="impl", role="dev")  # no carried failure
    prompt = _dev_prompt(task, store)
    assert "tool call rejected" not in prompt


def test_carry_forward_persists_and_clears_across_dispatches(
        tmp_errorta_home: Path) -> None:
    from errorta_council.coding.runner import _dev_prompt

    def _fetch(store: LedgerStore, task_id: str):
        return next(t for t in store.list_tasks() if t.task_id == task_id)

    store = _store("s17rt")
    task = store.add_task(title="impl", role="dev")
    # A rejection requeues the task with the failure recorded.
    carried = store.update_task(
        task.task_id, state="todo",
        last_tool_failure="tool_not_allowed: 'read_files' — use a "
        "context_request intent")
    assert carried.last_tool_failure  # persisted on the task
    assert "read_files" in _dev_prompt(_fetch(store, task.task_id), store)
    # A successful write clears it.
    store.update_task(task.task_id, last_tool_failure="")
    assert "tool call rejected" not in _dev_prompt(_fetch(store, task.task_id), store)
