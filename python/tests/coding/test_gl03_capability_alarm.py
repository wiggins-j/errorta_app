"""GL03 — the hallucinated-tool-call alarm + grant-or-delete audit.

GL03 treats a confabulated (ungranted) tool call as a capability-gap SIGNAL, not
just a logged failure. It extends the committed SPEC-15 manifest and SPEC-17
tool-catalog: an agent that keeps inventing a tool it was not granted is telling
you what interface the task needs. These tests lock:

* Item 1 — the pure detector (``confabulation_from_failure``) truth table: gap vs
  typo vs write-failure, and the per-role read flag; the runner wiring
  (threshold + one deduped alarm + a ``tool_confabulation`` decision + the PM
  planner-feedback note); the 352-storm dedupe lock.
* Item 2 — the grant-or-delete audit: green on the committed-tree-shaped manifest,
  red with a legible message on a read-stripped REVIEWER or a gate-less TESTER.

HONESTY (spec §7): the confabulation→gap link is the report's weakest match —
inferred from ACI results, not directly studied. The detector is an advisory
heuristic, tuned conservatively (it pages the PM; it never blocks the run).
"""
from __future__ import annotations

import pytest

from errorta_council.coding import attention, capabilities
from errorta_council.coding.schemas import TurnErrorCode
from errorta_council.coding.topology import DEV, PM, REVIEWER, TESTER

_NOT_ALLOWED = TurnErrorCode.tool_not_allowed.value


# --------------------------------------------------------------------------- #
# Manifest builders (RoleCapability constructed directly — the detector is pure).
# --------------------------------------------------------------------------- #

def _cap(role, *, tools=(), repo_read=False, can_execute=False, gate=False,
         dispatch=None):
    # SPEC-26: `dispatch` (a UNIT-scoped test command exists) is what the audit's
    # TESTER arm now reads; `gate` (an acceptance gate exists) is what the
    # confabulation detector still reads. They default together so every existing
    # case here keeps its meaning; the tests that separate them say so.
    return capabilities.RoleCapability(
        role=role, tools=tools, repo_read=repo_read, can_execute=can_execute,
        gate_available=gate, summary="",
        can_dispatch=gate if dispatch is None else dispatch)


def _manifest(*, dev_read=False, reviewer_read=False, gate=False, dispatch=None):
    return {
        PM: _cap(PM, gate=gate, dispatch=dispatch),
        DEV: _cap(DEV, tools=("code_write",), repo_read=dev_read, gate=gate,
                  dispatch=dispatch),
        REVIEWER: _cap(REVIEWER, repo_read=reviewer_read, gate=gate,
                       dispatch=dispatch),
        TESTER: _cap(TESTER, gate=gate, dispatch=dispatch),
    }


def _reason(tool: str) -> str:
    """The SPEC-17 enriched tool_not_allowed reason for an invented tool."""
    return f"{_NOT_ALLOWED}: {tool} — this role executes only: code_write"


# --------------------------------------------------------------------------- #
# Item 1 — the detector truth table (pure). This table IS the spec of the detector.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("tool, dev_read, gate, is_gap, capability", [
    # Execution confabulation: run/test/exec/shell/launch/bench, no gate → gap.
    ("run_tests", False, False, True, "execution"),
    ("execute_tests", False, False, True, "execution"),
    ("shell", False, False, True, "execution"),
    ("launch_app", False, False, True, "execution"),
    ("benchmark", False, False, True, "execution"),
    # ... but if a gate exists, execution evidence has a home — SPEC-15 routes it.
    ("run_tests", False, True, False, None),
    # Read confabulation: read/grep/cat/open/ls, repo-read OFF → gap.
    ("read_files", False, False, True, "read"),
    ("grep_repo", False, False, True, "read"),
    ("open_file", False, False, True, "read"),
    # ... but with repo-read ON, Read was granted (SPEC-11/14): a naming typo, not gap.
    ("read_files", True, False, False, None),
    ("grep_repo", True, False, False, None),
    # A typo of the GRANTED tool (code_write) — SPEC-17's corrective hint, not a gap.
    ("code_writ", False, False, False, None),
    ("codewrite", False, False, False, None),
    # An unknown tool with no capability-intent signal — not a gap.
    ("frobnicate", False, False, False, None),
])
def test_detector_truth_table(tool, dev_read, gate, is_gap, capability) -> None:
    man = _manifest(dev_read=dev_read, gate=gate)
    sig = capabilities.confabulation_from_failure(DEV, tool, _reason(tool), man)
    assert sig.is_gap is is_gap, (tool, dev_read, gate)
    assert sig.capability == capability, (tool, dev_read, gate)


def test_write_failed_is_never_a_gap() -> None:
    # A code_write disk/parse failure is NOT a tool_not_allowed rejection — the
    # detector ignores it (spec §7: the signature is specifically confabulation).
    man = _manifest()
    for reason in ("coding_workspace_unavailable",
                   "invalid base64 in content_base64",
                   "src/app.py: No such file or directory"):
        sig = capabilities.confabulation_from_failure(
            DEV, "code_write", reason, man)
        assert sig.is_gap is False
        assert sig.capability is None


def test_read_gap_is_per_member_read_flag() -> None:
    # SPEC-17 edge: repo-read is per member. The SAME read_files call is a gap for a
    # member without Read and a typo for one with it.
    off = capabilities.confabulation_from_failure(
        DEV, "read_files", _reason("read_files"), _manifest(dev_read=False))
    on = capabilities.confabulation_from_failure(
        DEV, "read_files", _reason("read_files"), _manifest(dev_read=True))
    assert off.is_gap is True and off.capability == "read"
    assert on.is_gap is False


def test_detector_tolerates_missing_manifest_entry() -> None:
    # A defensive path: no manifest / no entry for the role → the capability is
    # treated as absent, so an execution/read confabulation still flags.
    sig = capabilities.confabulation_from_failure(
        "ghost", "run_tests", _reason("run_tests"), {})
    assert sig.is_gap is True and sig.capability == "execution"


# --------------------------------------------------------------------------- #
# Item 2 — the grant-or-delete audit.
# --------------------------------------------------------------------------- #

def test_audit_passes_on_committed_tree_shape() -> None:
    # SPEC-12 granted the TESTER the gate; SPEC-14 granted the REVIEWER repo-read.
    man = _manifest(reviewer_read=True, gate=True)
    assert capabilities.audit_grant_or_delete(man) == []


def test_audit_fails_on_read_stripped_reviewer() -> None:
    man = _manifest(reviewer_read=False, gate=True)
    violations = capabilities.audit_grant_or_delete(man)
    assert len(violations) == 1
    msg = violations[0]
    assert "REVIEWER" in msg and "verification" in msg
    assert "SPEC-14" in msg and "grant it" in msg and "delete" in msg


def test_audit_fails_on_gate_less_tester() -> None:
    man = _manifest(reviewer_read=True, gate=False)
    violations = capabilities.audit_grant_or_delete(man)
    assert len(violations) == 1
    msg = violations[0]
    assert "TESTER" in msg and "execution" in msg
    assert "SPEC-12" in msg


def test_audit_deleting_a_role_from_dispatch_is_the_other_half() -> None:
    # grant-OR-delete: an un-capable role absent from dispatch is NOT a violation.
    man = _manifest(reviewer_read=False, gate=False)
    # dispatched with the un-capable roles present → two violations.
    assert len(capabilities.audit_grant_or_delete(man)) == 2
    # dispatch only the DEV (write tool present) → clean.
    assert capabilities.audit_grant_or_delete(
        man, dispatched_roles=(DEV,)) == []


# --------------------------------------------------------------------------- #
# Item 1 — the runner wiring (threshold, one deduped alarm, decision, PM note).
# --------------------------------------------------------------------------- #

def _real_store(pid, tmp_path):
    from errorta_council.coding.ledger import LedgerStore
    s = LedgerStore(pid, root=tmp_path / f"l-{pid}")
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def _record_and_detect(store, task, tool, reason):
    """Faithfully replay the runner's recording path: the verbatim tool_failed
    decision is written FIRST, then GL03 detects alongside it (never rewriting it)."""
    from errorta_council.coding import runner
    store.record_decision(
        title=f"tool failed: {task.title}", context=f"task {task.task_id}",
        choice="tool_failed", rationale=f"{tool}: {reason}",
        related_task_ids=[task.task_id])
    runner._detect_tool_confabulation(store, task, DEV, tool, reason)


def _open_gap_alerts(store):
    return [s for s in attention.list_open(store.project_id, store=store)
            if s.source == "capability_gap"]


def _gap_decisions(store):
    return [d for d in store.list_decisions() if d["choice"] == "tool_confabulation"]


def test_single_confabulation_does_not_escalate(tmp_errorta_home, tmp_path) -> None:
    # The typo threshold: ONE stray ungranted call is a fat-finger, not a systematic
    # gap — no alarm, no decision, no PM note.
    from errorta_council.coding import runner
    s = _real_store("gl03a", tmp_path)
    task = s.add_task(role="dev", title="Add gravity solver")
    _record_and_detect(s, task, "run_tests", _reason("run_tests"))
    assert _open_gap_alerts(s) == []
    assert _gap_decisions(s) == []
    assert runner._capability_gap_note(s) == ""


def test_repeated_execution_confabulation_escalates_once(
        tmp_errorta_home, tmp_path) -> None:
    # The gravity-golf signature: a write-only DEV keeps inventing a "run tests"
    # tool. Past the threshold → exactly ONE deduped alarm + ONE decision + a PM note.
    from errorta_council.coding import runner
    s = _real_store("gl03b", tmp_path)
    task = s.add_task(role="dev", title="Run acceptance gate and fix failures")
    for _ in range(5):  # the 352-storm, in miniature
        _record_and_detect(s, task, "run_tests", _reason("run_tests"))
    alerts = _open_gap_alerts(s)
    assert len(alerts) == 1                     # ONE alarm, not five
    assert alerts[0].kind == "alert" and not alerts[0].blocking
    assert alerts[0].context["role"] == DEV
    assert alerts[0].context["capability"] == "execution"
    decisions = _gap_decisions(s)
    assert len(decisions) == 1                  # ONE decision, not five
    assert decisions[0]["role"] == DEV
    assert decisions[0]["capability"] == "execution"
    assert decisions[0]["tool_name"] == "run_tests"
    # The verbatim tool_failed ledger events are still all there (not rewritten).
    assert sum(1 for d in s.list_decisions() if d["choice"] == "tool_failed") == 5
    # The PM's next composed prompt names the role + capability + tool.
    note = runner._capability_gap_note(s)
    assert "dev" in note and "execution" in note and "run_tests" in note
    assert "re-plan" in note or "re-dispatch" in note.lower()


def test_pm_prompt_surfaces_the_gap_note(tmp_errorta_home, tmp_path) -> None:
    from errorta_council.coding import runner
    s = _real_store("gl03c", tmp_path)
    task = s.add_task(role="dev", title="Run acceptance gate and fix failures")
    for _ in range(2):
        _record_and_detect(s, task, "run_tests", _reason("run_tests"))
    prompt = runner._pm_prompt(s)
    assert "run_tests" in prompt and "execution" in prompt


def test_typo_of_granted_tool_never_escalates(tmp_errorta_home, tmp_path) -> None:
    # code_writ repeated many times is SPEC-17's corrective-hint territory, NOT a
    # capability gap — no alarm even past the threshold.
    from errorta_council.coding import runner
    s = _real_store("gl03d", tmp_path)
    task = s.add_task(role="dev", title="Add gravity solver")
    for _ in range(5):
        _record_and_detect(s, task, "code_writ", _reason("code_writ"))
    assert _open_gap_alerts(s) == []
    assert _gap_decisions(s) == []
    assert runner._capability_gap_note(s) == ""


def test_write_failed_on_granted_tool_is_not_a_confabulation(
        tmp_errorta_home, tmp_path) -> None:
    # A real write_failed on code_write (a GRANTED tool) — disk/parse error, never a
    # tool_not_allowed rejection — is not a confabulation no matter how it repeats.
    from errorta_council.coding import runner
    s = _real_store("gl03e", tmp_path)
    task = s.add_task(role="dev", title="Add gravity solver")
    for _ in range(5):
        # Record as a write_failed decision (not tool_failed) and detect on the raw
        # disk reason — the detector must ignore it.
        s.record_decision(
            title="write failed", context=f"task {task.task_id}",
            choice="write_failed", rationale="src/app.py: No such file or directory",
            related_task_ids=[task.task_id])
        runner._detect_tool_confabulation(
            s, task, DEV, "code_write", "src/app.py: No such file or directory")
    assert _open_gap_alerts(s) == []
    assert _gap_decisions(s) == []


def test_read_gap_escalates_when_repo_read_off(tmp_errorta_home, tmp_path) -> None:
    # dev_repo_read defaults OFF, so a repeated read_files confabulation is a read
    # capability gap.
    s = _real_store("gl03f", tmp_path)
    task = s.add_task(role="dev", title="Wire the loader")
    for _ in range(2):
        _record_and_detect(s, task, "read_files", _reason("read_files"))
    alerts = _open_gap_alerts(s)
    assert len(alerts) == 1
    assert alerts[0].context["capability"] == "read"
    assert _gap_decisions(s)[0]["capability"] == "read"
