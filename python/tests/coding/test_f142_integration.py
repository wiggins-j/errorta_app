"""F142 Slice 4 — integration + regression locks.

Slices 1-3 changed prompt wording (WS-A reviewer scope), the foundation gate
(WS-B script-style), and the tester applicability flag (WS-C). This module locks
the END-TO-END behavior and PROVES the earlier slices did not weaken any real
protection. It reuses the harnesses from `test_f139_part_b.py` (store/ws/merge
helpers, the `build_run_turn` reviewer path, `runtime_cap`, `next_tasks`) rather
than building a new engine harness.

Acceptance criteria covered (see `docs/specs/F142-task-scoped-pr-review.md`):

- AC4 (honest parallelism): a `new` project with INDEPENDENT tasks + a manifest
  lifts the clamp to >1 and has >=2 tasks simultaneously dependency-ready once
  the foundation merges; a strictly-LINEAR single-file project makes SERIAL
  progress (one ready task at a time, cap ramps but only one dev can run) to
  completion with NO `foundation_not_converging` decision once its single .py
  merges. Serial is correct there — concurrency is NOT asserted.
- AC6 (regression guards): (a) a reviewer that returns a blocking finding on a
  PR that deletes/breaks an existing merged function still records
  `changes_requested` and (for a contract-mismatch finding) still spawns the
  WS-D2 contract-owner task; (b) forward contract drift between two in-flight
  PRs still routes through `_contract_owner_for` despite WS-A's new
  "incompleteness is not a reason" wording.
- AC7 (governance unaffected): a strict-governance task materialized from an
  approved plan slice with `done_when` produces a reviewer prompt that points at
  that `done_when` — verified through the REAL governance store + materialize +
  `_review_project_context` path (Slice 1's unit test only used a bare Task).
"""
import json
from pathlib import Path

from errorta_council.coding.autonomy import (
    CodingAutonomyPolicy,
    effective_parallelism,
    runtime_cap,
)
from errorta_council.coding.governance import GovernanceStore
from errorta_council.coding.governance_materialize import materialize_approved_plan
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _CONTRACT_OWNER_TITLE,
    _contract_owner_for,
    _findings_show_contract_mismatch,
    _review_pr_prompt,
    _review_project_context,
    build_run_turn,
    foundation_ready,
    members_by_coding_role,
    refresh_foundation_status,
)
from errorta_council.coding.topology import DEV, REVIEWER, Assign
from errorta_council.coding.workspace import CodingWorkspace

# 3 non-PM members -> static parallelism 3 (matches test_f139_part_b.py).
_MEMBERS = [("m-pm", "pm"), ("m-dev-1", "dev"), ("m-dev-2", "dev"),
            ("m-rev", "reviewer")]


def _store(pid: str, tmp_path: Path, *, target: str = "new") -> LedgerStore:
    s = LedgerStore(pid, root=tmp_path / f"ledger-{pid}")
    s.create_project(north_star="n", definition_of_done="d", target=target,
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


def _record_merged_pr(store: LedgerStore, tid: str) -> None:
    pr = store.record_pr(task_id=tid, branch=f"task-{tid}", head=f"h-{tid}",
                         dev_member="m")
    store.update_pr(pr["pr_id"], status="merged", head=f"h-{tid}")


def _n_foundation_alerts(store: LedgerStore) -> int:
    return sum(1 for d in store.list_decisions()
               if d["choice"] == "foundation_not_converging")


# --- AC4: honest parallelism ------------------------------------------------


def test_ac4_independent_tasks_with_manifest_run_concurrently(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """A `new` project with INDEPENDENT dev tasks + a build manifest: once the
    foundation merges the clamp lifts above 1 AND >=2 tasks are simultaneously
    dependency-ready, so >=2 devs can run concurrently. We assert the cap math +
    ready-task selection (the two facts the scheduler needs) rather than spinning
    a live multi-model loop."""
    s = _store("p4a", tmp_path)
    ws = _ws("p4a", s)
    policy = CodingAutonomyPolicy()  # AUTO
    assert effective_parallelism(policy, _MEMBERS) == 3

    # Before the foundation merges the clamp holds at 1.
    assert refresh_foundation_status(s, ws) == "pending"
    assert runtime_cap(policy, _MEMBERS, s) == 1

    # Foundation merges: a manifest + a source entrypoint on master.
    _merge_file(ws, "t-foundation", "package.json", '{"name": "x"}\n')
    _merge_file(ws, "t-entry", "src/index.tsx", "export const App = 1\n")
    assert foundation_ready(s, ws) is True
    assert refresh_foundation_status(s, ws) == "merged"
    _record_merged_pr(s, "foundation")

    # The clamp lifts: cap ramps above 1 (ramp -> 2 after the first merge).
    assert runtime_cap(policy, _MEMBERS, s) > 1

    # INDEPENDENT tasks (no depends_on chain) are simultaneously ready, so the
    # scheduler can hand distinct tasks to >=2 idle devs in one batch.
    a = s.add_task(title="feature A (independent)", role=DEV)
    b = s.add_task(title="feature B (independent)", role=DEV)
    ready = s.next_tasks(DEV, 5)
    ready_ids = {t.task_id for t in ready}
    assert a.task_id in ready_ids and b.task_id in ready_ids
    assert len(ready) >= 2  # >=2 devs have distinct work at once


def test_ac4_linear_single_file_project_makes_serial_progress(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """A strictly-LINEAR single-file project: each task edits the one file and
    `depends_on` the prior. It runs ~1-at-a-time BY DEPENDENCY SHAPE (serial is
    correct), and once the single `game.py` merges the WS-B script-style gate
    flips foundation off `pending` so NO `foundation_not_converging` decision is
    recorded. We assert serial progress + completion, NOT concurrency."""
    s = _store("p4b", tmp_path)
    ws = _ws("p4b", s)

    # A single-file North Star: game.py is the sole deliverable, no manifest.
    _merge_file(ws, "t0", "game.py", "PI = 3.14\n")
    assert foundation_ready(s, ws) is True             # WS-B script-style gate
    assert refresh_foundation_status(s, ws) == "merged"
    assert _n_foundation_alerts(s) == 0                # gate opened -> no false alert

    # A strictly-linear task chain: at most ONE task is ever dependency-ready.
    t1 = s.add_task(title="add Move class to game.py", role=DEV)
    t2 = s.add_task(title="add Creature class to game.py", role=DEV,
                    depends_on=[t1.task_id])
    t3 = s.add_task(title="add Battle loop to game.py", role=DEV,
                    depends_on=[t2.task_id])

    # Only t1 is ready now (t2/t3 gated on their predecessor).
    ready = s.next_tasks(DEV, 5)
    assert [t.task_id for t in ready] == [t1.task_id]

    # Complete t1 -> only t2 becomes ready. Serial progress, one file at a time.
    s.update_task(t1.task_id, state="done")
    ready = s.next_tasks(DEV, 5)
    assert [t.task_id for t in ready] == [t2.task_id]
    s.update_task(t2.task_id, state="done")
    ready = s.next_tasks(DEV, 5)
    assert [t.task_id for t in ready] == [t3.task_id]
    s.update_task(t3.task_id, state="done")
    assert s.next_tasks(DEV, 5) == []                  # chain drained -> completion

    # The run never stalled the clamp on: foundation stays merged, no false alert
    # ever fired even though the project is inherently serial.
    assert refresh_foundation_status(s, ws) == "merged"
    assert _n_foundation_alerts(s) == 0


# --- AC6(a): delete/break/import-absent still blocks -------------------------


def test_ac6a_reviewer_blocking_finding_still_requests_changes(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """A PR that breaks an existing merged function: given a reviewer that
    returns a blocking finding, the FULL apply path still records
    `changes_requested`, persists the findings, opens a revise task, and records
    a `review_rejected` decision. Slice 1 only changed prompt WORDING — this
    locks that the reviewer-returns-blocking -> changes_requested code path is
    untouched."""
    s = _store("p6a", tmp_path)
    ws = _ws("p6a", s)
    # An existing merged function lives on master.
    _merge_file(ws, "t-base", "lib.py", "def greet():\n    return 'hi'\n")

    # A dev PR that DELETES/BREAKS greet() and imports a symbol absent from master.
    dev_task = s.add_task(title="use greeter", role=DEV)
    branch = ws.start_task_branch(dev_task.task_id)
    ws.write_file("app.py", "from lib import farewell\nprint(farewell())\n",
                  task_id=dev_task.task_id)
    ws.write_file("lib.py", "# greet() removed\n", task_id=dev_task.task_id)
    pr = s.record_pr(task_id=dev_task.task_id, branch=branch,
                     head=ws.branch_head(branch), dev_member="m-dev-1")
    review_task = s.add_task(title=f"review PR: {branch}", role=REVIEWER,
                             pr_id=pr["pr_id"])

    def caller(_member, _prompt):
        return json.dumps({
            "schema_version": "coding_turn.v1", "role": "reviewer",
            "task_id": review_task.task_id,
            "intent": {"kind": "review_verdict", "reviewed_head": pr["head"],
                       "approved": False, "findings": [
                {"severity": "blocking",
                 "title": "imports farewell, absent from master; removes greet()",
                 "body": "app.py imports a symbol not on master and deletes an "
                         "existing merged function",
                 "path": "app.py"}]}})

    rt = build_run_turn(s, ws, members_by_coding_role([
        {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}}]),
        caller, guardrail_enabled=True)
    out = rt(Assign(member_id="m-rev", task_id=review_task.task_id, role=REVIEWER), s)

    assert out.kind == "pr_reviewed"
    pr_after = s.get_pr(pr["pr_id"])
    assert pr_after["status"] == "changes_requested"
    assert pr_after["reviewer_approved"] is False
    assert pr_after["review_findings"]  # findings persisted (F126)
    # A revise task was opened so the dev must fix it (nothing merges).
    assert any(t.title.startswith("revise:") for t in s.list_tasks())
    assert any(d["choice"] == "review_rejected" for d in s.list_decisions())


def test_reviewer_prompt_carries_DEV_task_scope_not_review_task(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """Regression lock (review finding A1/A2): in the REAL reviewer path `task` is
    the reviewer's own 'review PR: ...' task (empty detail). The scope the PR must
    satisfy belongs to the DEV task (pr['task_id']). Drive build_run_turn and
    capture the prompt actually handed to the reviewer member: it MUST contain the
    DEV task's distinctive detail as the scope, and must NOT present the bare
    'review PR:' review-task title as the scope. (Unit tests that call
    _review_pr_prompt with the dev task directly cannot catch this.)"""
    s = _store("p_scope", tmp_path)
    ws = _ws("p_scope", s)
    dev_detail = "SCOPESENTINEL implement ONLY the Move dataclass in game.py"
    dev_task = s.add_task(title="add Move dataclass", role=DEV, detail=dev_detail)
    branch = ws.start_task_branch(dev_task.task_id)
    ws.write_file("game.py", "class Move:\n    pass\n", task_id=dev_task.task_id)
    pr = s.record_pr(task_id=dev_task.task_id, branch=branch,
                     head=ws.branch_head(branch), dev_member="m-dev-1")
    review_task = s.add_task(title=f"review PR: {branch}", role=REVIEWER,
                             pr_id=pr["pr_id"])

    seen = {}

    def caller(_member, _prompt):
        seen["prompt"] = _prompt
        return json.dumps({
            "schema_version": "coding_turn.v1", "role": "reviewer",
            "task_id": review_task.task_id,
            "intent": {"kind": "review_verdict", "reviewed_head": pr["head"],
                       "approved": True, "findings": []}})

    rt = build_run_turn(s, ws, members_by_coding_role([
        {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}}]),
        caller, guardrail_enabled=True)
    rt(Assign(member_id="m-rev", task_id=review_task.task_id, role=REVIEWER), s)

    prompt = seen["prompt"]
    # The DEV task's scope (detail) reached the reviewer.
    assert dev_detail in prompt
    # The review task's own title is NOT presented as the PR's scope.
    assert f"ONE task: review PR: {branch}" not in prompt


def test_ac6a_contract_mismatch_finding_spawns_contract_owner(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """A blocking finding that also reads as a contract mismatch (imports a
    symbol absent from master against a shared type) both requests changes AND
    fires the WS-D2 contract-owner task through the real reviewer turn. Locks
    that Slice 1 did not sever the changes_requested -> `_contract_owner_for`
    edge."""
    s = _store("p6a2", tmp_path)
    ws = _ws("p6a2", s)
    dev_task = s.add_task(title="build post card", role=DEV)
    branch = ws.start_task_branch(dev_task.task_id)
    ws.write_file("PostCard.tsx", "export const PostCard = 1\n",
                  task_id=dev_task.task_id)
    pr = s.record_pr(task_id=dev_task.task_id, branch=branch,
                     head=ws.branch_head(branch), dev_member="m-dev-1")
    review_task = s.add_task(title=f"review PR: {branch}", role=REVIEWER,
                             pr_id=pr["pr_id"])

    def caller(_member, _prompt):
        return json.dumps({
            "schema_version": "coding_turn.v1", "role": "reviewer",
            "task_id": review_task.task_id,
            "intent": {"kind": "review_verdict", "reviewed_head": pr["head"],
                       "approved": False, "findings": [
                {"severity": "blocking",
                 "title": "Post type import absent from master",
                 "body": "PostCard imports a Post type that does not match the "
                         "merged Post type",
                 "path": "PostCard.tsx"}]}})

    rt = build_run_turn(s, ws, members_by_coding_role([
        {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}}]),
        caller, guardrail_enabled=True)
    rt(Assign(member_id="m-rev", task_id=review_task.task_id, role=REVIEWER), s)

    assert s.get_pr(pr["pr_id"])["status"] == "changes_requested"
    titles = [t.title for t in s.list_tasks()]
    assert _CONTRACT_OWNER_TITLE in titles           # WS-D2 owner spawned
    revise = next(t for t in s.list_tasks() if t.title.startswith("revise:"))
    owner = next(t for t in s.list_tasks() if t.title == _CONTRACT_OWNER_TITLE)
    assert owner.task_id in (revise.depends_on or [])  # revise waits on the owner


# --- AC6(b): forward contract drift still routes through WS-D2 ---------------


def test_ac6b_incompatible_shared_type_between_inflight_prs_fires_owner(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """Two in-flight PRs defining an INCOMPATIBLE shared type. When the reviewer
    of the second returns a finding whose text matches the contract-mismatch
    detector, `_contract_owner_for` fires. This locks that WS-A's new
    "incompleteness is not a reason" wording did NOT wave forward contract drift
    through."""
    s = _store("p6b", tmp_path)
    # PR #1 defines a shared `User` type one way (in flight, not merged).
    t1 = s.add_task(title="define User type in api.ts", role=DEV)
    s.record_pr(task_id=t1.task_id, branch="task-user-a", head="h1",
                dev_member="m-dev-1")
    # PR #2 defines an INCOMPATIBLE `User` shape; its reviewer flags the drift.
    t2 = s.add_task(title="define User type in profile.ts", role=DEV)
    pr2 = s.record_pr(task_id=t2.task_id, branch="task-user-b", head="h2",
                      dev_member="m-dev-2")

    drift = [{"severity": "blocking",
              "title": "User type does not match the in-flight User interface",
              "body": "profile.ts declares an incompatible shared User type"}]
    # The detector recognizes the drift (a shared-contract noun co-occurs).
    assert _findings_show_contract_mismatch(drift) is True
    # And the owner-centralization path fires for the second PR.
    owner_id = _contract_owner_for(s, pr2, drift)
    assert owner_id is not None
    assert _CONTRACT_OWNER_TITLE in [t.title for t in s.list_tasks()]


# --- AC7: governance unaffected ---------------------------------------------


def test_ac7_strict_governance_reviewer_prompt_points_at_done_when(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """AC7 at the INTEGRATION level: a strict-governance task materialized from a
    real approved plan slice carrying `done_when` produces a reviewer prompt
    (built via `_review_project_context` + `_review_pr_prompt`) that surfaces
    that `done_when` as the acceptance bar. Slice 1's unit test used a bare Task
    with no governance store; this drives the real materialize + governance
    context read end to end, proving F100 behavior is identical."""
    s = _store("p7", tmp_path)
    ws = _ws("p7", s)
    governance = GovernanceStore.for_ledger(s)
    governance.update_state(mode="strict", phase="development")
    governance.append_artifact(kind="spec", title="Spec", body_markdown="spec body",
                               state="approved")
    governance.append_artifact(
        kind="implementation_plan", title="Plan", state="approved",
        body_json={"slices": [{
            "slice_id": "S1", "title": "Scaffold game.py",
            "done_when": ["game.py exists with a Move class"],
            "tests": ["pytest"], "review_focus": ["module shape"]}]})

    result = materialize_approved_plan(s, governance)
    assert result["created"] == 1
    task = next(t for t in s.list_tasks() if t.source_slice_id == "S1")
    assert task.governance_required is True

    # A PR under review for that governance task.
    pr = s.record_pr(task_id=task.task_id, branch="task-gov", head="hg",
                     dev_member="m-dev-1")
    ctx = _review_project_context(s, ws, pr)
    prompt = _review_pr_prompt(task, pr, diff="+class Move: ...\n",
                               project_context=ctx)

    # The reviewer prompt points the acceptance bar at the slice done_when, not
    # the North Star, and identifies the task as governance-sourced.
    assert "governance-sourced" in prompt
    assert "done_when" in prompt
    assert "game.py exists with a Move class" in prompt
    # WS-A scope framing is still present (governance path is not a regression).
    assert "ONE scoped task" in prompt
