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
