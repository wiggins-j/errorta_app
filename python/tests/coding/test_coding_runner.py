"""F087 live-integration: drive the autonomy loop against a fake gateway.

F087-17: the team works branch-per-task and integrates via PM-approved PRs into
master, so work ACCUMULATES (no clobbering) and a blind reviewer can't land a
regression (a PR merges only when reviewer-approved AND tests-green).
"""
import json
import re
import sys
from pathlib import Path

from errorta_council.coding.autonomy import (
    CADENCE_OFF,
    DEFINITION_OF_DONE,
    CodingAutonomyPolicy,
)
from errorta_council.coding.governance import GovernanceStore
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    CodingRunner,
    _prune_dead_branches,
    _reconcile_stale,
    _supersede_ancestors,
    build_run_turn,
    members_by_coding_role,
)
from errorta_council.coding.topology import GovernanceReview, Plan

MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]


def _task_id(prompt: str, role: str) -> str:
    return re.search(rf"{role} for task id '([^']+)'", prompt).group(1)


def _pr_head(prompt: str) -> str:
    return re.search(r"PR head you are reviewing is '([^']*)'", prompt).group(1)


def _delivery_head(prompt: str) -> str:
    # F146 Slice B: the delivery-review prompt echoes the integrated head.
    return re.search(r"delivered head you are reviewing is '([^']*)'", prompt).group(1)


def _pm_env(*, tasks=None, done=False, completion_summary="") -> str:
    intent = {"kind": "plan", "done": done}
    if tasks is not None:
        intent["tasks"] = tasks
    if completion_summary:
        intent["completion_summary"] = completion_summary
    return json.dumps({"schema_version": "coding_turn.v1", "role": "pm", "intent": intent})


def _dev_env(task_id: str, files) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "dev", "task_id": task_id,
        "intent": {"kind": "tool_plan", "task_type": "implementation",
                   "tool_calls": [{"tool": "code_write",
                                   "args": {"path": p, "content": c}}
                                  for p, c in files]}})


def _rev_env(task_id: str, head: str, *, approved=True, findings=None) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "reviewer", "task_id": task_id,
        "intent": {"kind": "review_verdict", "reviewed_head": head,
                   "approved": approved, "findings": findings or []}})


def _tester_env(task_id: str, command_ids) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "tester", "task_id": task_id,
        "intent": {"kind": "test_plan", "command_ids": command_ids,
                   "scope": "full_project", "rationale": "run"}})


_ADD = "def add(a, b):\n    return a + b\n"
_SUB = "def subtract(a, b):\n    return a - b\n"


class FakeGateway:
    """PM plans add() then subtract(); the dev EXTENDS using read-back; reviewer
    approves; tester runs the real unit command."""
    def __init__(self) -> None:
        self.pm_calls = 0

    def __call__(self, member: dict, prompt: str) -> str:
        if "You are the PM" in prompt:
            self.pm_calls += 1
            if self.pm_calls == 1:
                return _pm_env(tasks=[{"title": "implement add", "role": "dev"}])
            if self.pm_calls == 2:
                return _pm_env(tasks=[{"title": "implement subtract", "role": "dev"}])
            return _pm_env(done=True, completion_summary="add + subtract done")
        if "You are a developer" in prompt:
            tid = _task_id(prompt, "developer")
            # read-back: if add() is already present, EXTEND with subtract()
            if "def add" in prompt:
                return _dev_env(tid, [("calc.py", _ADD + "\n" + _SUB)])
            return _dev_env(tid, [("calc.py", _ADD)])
        if "DELIVERY reviewer" in prompt:
            # F146 Slice B: approve the integrated delivered head so the run can
            # complete (delivery tests run deterministically, no fake needed).
            return _rev_env("delivery-review", _delivery_head(prompt), approved=True)
        if "You are a reviewer" in prompt:
            return _rev_env(_task_id(prompt, "reviewer"), _pr_head(prompt), approved=True)
        if "You are a tester" in prompt:
            return _tester_env(_task_id(prompt, "tester"), ["unit"])
        return "{}"


def test_full_pr_flow_accumulates_and_completes(tmp_errorta_home: Path) -> None:
    store = LedgerStore("prflow")
    store.create_project(north_star="calc with add+subtract",
                         definition_of_done="both work + tested",
                         target="new", repo_path=None)
    # A real test command (asserts add() — the MVP that exists from PR1 on, so
    # both incremental PRs go green). Accumulation is proven by master ending
    # with BOTH functions even though each PR was a separate branch/merge.
    store.set_test_commands({"unit": {
        "argv": [sys.executable, "-c",
                 "import sys; sys.path.insert(0,'.'); from calc import add; "
                 "assert add(1,2)==3"],
        "cwd": ".", "timeout_seconds": 30}})
    runner = CodingRunner("prflow", MEMBERS, FakeGateway(), guardrail_enabled=True)
    res = runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_iterations=40))

    assert res.stop_reason == DEFINITION_OF_DONE
    # two PRs, both merged
    prs = store.list_prs()
    assert len(prs) == 2
    assert all(p["status"] == "merged" for p in prs)
    assert all(p["reviewer_approved"] and p["tests_passed"] for p in prs)
    # master ACCUMULATED both functions (no clobber)
    runner.workspace.checkout("master")
    final = runner.workspace._ws.read_file("calc.py")
    assert "def add" in final and "def subtract" in final
    # the second PR's test ran green over BOTH ops
    runs = store.list_test_runs()
    assert any(r["passed"] for r in runs)
    # F093: the PM's completion summary is persisted + projects through to_dict
    proj = store.get_project()
    assert proj.completion_summary == "add + subtract done"
    assert proj.completed_at
    assert proj.to_dict()["completion_summary"] == "add + subtract done"


def test_set_completion_round_trips(tmp_errorta_home: Path) -> None:
    store = LedgerStore("setcompl")
    p0 = store.create_project(north_star="n", definition_of_done="d",
                              target="new", repo_path=None)
    store.set_completion("done because X")
    proj = store.get_project()
    assert proj.completion_summary == "done because X"
    assert proj.completed_at
    assert proj.revision == p0.revision + 1


def test_pr_flow_merges_without_test_commands(tmp_errorta_home: Path) -> None:
    """Greenfield projects start with NO registered test commands. The merge gate
    must not require tests-green in that case (there's nothing to run) — review
    approval alone lands the PR. Regression: without this the team got reviewers
    APPROVING but NOTHING ever merged (tests_passed was never set), so master
    stayed empty and the run churned forever in a revise loop."""
    store = LedgerStore("prnotest")
    store.create_project(north_star="calc with add+subtract",
                         definition_of_done="both work",
                         target="new", repo_path=None)
    # NOTE: deliberately NO store.set_test_commands(...) -> empty registry.
    runner = CodingRunner("prnotest", MEMBERS, FakeGateway(), guardrail_enabled=True)
    res = runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_iterations=40))

    assert res.stop_reason == DEFINITION_OF_DONE
    prs = store.list_prs()
    assert len(prs) == 2
    # both merged on review-approval alone (no tester gate)
    assert all(p["status"] == "merged" for p in prs), [p["status"] for p in prs]
    assert all(p["reviewer_approved"] for p in prs)
    # no tester tasks were ever created — nothing to run
    assert not any(t.role == "tester" for t in store.list_tasks())
    assert store.list_test_runs() == []
    # master ACCUMULATED both functions (the work actually landed)
    runner.workspace.checkout("master")
    final = runner.workspace._ws.read_file("calc.py")
    assert "def add" in final and "def subtract" in final


def test_reviewer_rejection_blocks_merge(tmp_errorta_home: Path) -> None:
    store = LedgerStore("prrej")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)
    store.set_test_commands({"unit": {"argv": [sys.executable, "-c", "pass"],
                                      "cwd": ".", "timeout_seconds": 30}})

    class RejectRev(FakeGateway):
        def __call__(self, member, prompt):
            if "You are a reviewer" in prompt:
                return _rev_env(_task_id(prompt, "reviewer"), _pr_head(prompt),
                                approved=False,
                                findings=[{"severity": "major", "path": "calc.py",
                                           "title": "nope"}])
            return super().__call__(member, prompt)

    runner = CodingRunner("prrej", MEMBERS, RejectRev(), guardrail_enabled=True)
    runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_iterations=8))
    prs = store.list_prs()
    # the PR was NOT merged (reviewer rejected) -> changes_requested, never merged
    assert prs and all(p["status"] != "merged" for p in prs)
    assert any(p["reviewer_approved"] is False for p in prs)
    assert any(d["choice"] == "review_rejected" for d in store.list_decisions())


def test_red_tests_block_merge(tmp_errorta_home: Path) -> None:
    store = LedgerStore("prred")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)
    # a RED command (always exits 1) -> PR can never become mergeable
    store.set_test_commands({"unit": {"argv": [sys.executable, "-c", "import sys; sys.exit(1)"],
                                      "cwd": ".", "timeout_seconds": 30}})
    runner = CodingRunner("prred", MEMBERS, FakeGateway(), guardrail_enabled=True)
    runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_iterations=8))
    prs = store.list_prs()
    assert prs and all(p["status"] != "merged" for p in prs)
    assert any(p["tests_passed"] is False for p in prs)
    assert any(d["choice"] == "tested_fail" for d in store.list_decisions())


def test_dev_malformed_turn_requeues(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.topology import DEV, Assign
    store = LedgerStore("prbad")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)
    task = store.add_task(title="impl", role=DEV)

    def caller(member, prompt):
        return "not json"

    rt = build_run_turn(store, _real_ws("prbad", store),
                        members_by_coding_role(MEMBERS), caller, guardrail_enabled=True)
    rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)
    assert store.list_tasks(state="todo")  # requeued, not done
    assert store.list_prs() == []           # no PR opened
    assert any(d["choice"] == "dev_turn_rejected" for d in store.list_decisions())


def test_dev_schema_rejection_gets_corrective_retry_then_success(
    tmp_errorta_home: Path,
) -> None:
    from errorta_council.coding.topology import DEV, Assign
    store = LedgerStore("prretry")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)
    task = store.add_task(title="impl", role=DEV)
    prompts: list[str] = []

    def caller(member, prompt):
        prompts.append(prompt)
        tid = _task_id(prompt, "developer")
        if len(prompts) == 1:
            return json.dumps({
                "schema_version": "coding_turn.v1",
                "role": "dev",
                "task_id": tid,
                "intent": {
                    "kind": "tool_plan",
                    "task_type": "implementation",
                    "tool_calls": [],
                    "summary": "I would write calc.py",
                },
            })
        return _dev_env(tid, [("calc.py", _ADD)])

    rt = build_run_turn(store, _real_ws("prretry", store),
                        members_by_coding_role(MEMBERS), caller, guardrail_enabled=True)
    outcome = rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)

    assert outcome.kind == "pr_opened"
    assert outcome.model_calls == 2
    assert len(prompts) == 2
    assert "Your previous coding_turn.v1 response was rejected" in prompts[1]
    assert any(d["choice"] == "dev_turn_correction_retry" for d in store.list_decisions())
    assert not any(d["choice"] == "dev_turn_rejected" for d in store.list_decisions())
    assert store.list_turns()[-1]["parse_ok"] is True


def test_dev_schema_rejection_retry_cap_requeues_honest_noop(
    tmp_errorta_home: Path,
) -> None:
    from errorta_council.coding.topology import DEV, Assign
    store = LedgerStore("prretrybad")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)
    task = store.add_task(title="impl", role=DEV)
    prompts: list[str] = []

    def caller(member, prompt):
        prompts.append(prompt)
        return json.dumps({
            "schema_version": "coding_turn.v1",
            "role": "dev",
            "task_id": task.task_id,
            "intent": {
                "kind": "tool_plan",
                "task_type": "implementation",
                "tool_calls": [],
            },
        })

    rt = build_run_turn(store, _real_ws("prretrybad", store),
                        members_by_coding_role(MEMBERS), caller, guardrail_enabled=True)
    outcome = rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)

    assert outcome.kind == "noop"
    assert outcome.model_calls == 3
    assert len(prompts) == 3  # F127: workers get 2 corrective retries
    assert store.list_tasks(state="todo")
    assert store.list_prs() == []
    assert any(d["choice"] == "dev_turn_correction_retry" for d in store.list_decisions())
    assert any(d["choice"] == "dev_turn_rejected" for d in store.list_decisions())
    assert store.list_turns()[-1]["parse_ok"] is False


def test_dev_disallowed_tool_turn_is_unproductive_not_infinite_loop(
    tmp_errorta_home: Path,
) -> None:
    """F136: a dev turn whose only tool call is disallowed (zero usable writes)
    must feed the F127 escalate-up ladder (unproductive=True), not requeue as a
    plain noop forever. Reproduces the live reddit-look-a-like loop where one
    task logged 352 identical `MemRead: tool_not_allowed` failures."""
    from errorta_council.coding.topology import DEV, Assign
    store = LedgerStore("prdenyloop")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)
    task = store.add_task(title="Fix PR branch changes requested", role=DEV)

    def caller(member, prompt):
        return json.dumps({
            "schema_version": "coding_turn.v1", "role": "dev",
            "task_id": task.task_id,
            "intent": {"kind": "tool_plan", "task_type": "implementation",
                       "tool_calls": [{"tool": "MemRead",
                                       "args": {"ref": "mem:whatever"}}]}})

    rt = build_run_turn(store, _real_ws("prdenyloop", store),
                        members_by_coding_role(MEMBERS), caller, guardrail_enabled=True)
    outcome = rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)

    assert outcome.kind == "noop"
    assert outcome.unproductive is True            # the fix: feeds the F127 ladder
    assert outcome.member_id == "m-dev"
    assert outcome.member_role == DEV
    assert store.list_tasks(state="todo")          # requeued, not done
    assert store.list_prs() == []
    assert any(d["choice"] == "tool_failed" for d in store.list_decisions())


def test_dev_partial_progress_turn_is_not_penalised(tmp_errorta_home: Path) -> None:
    """F136: a turn where SOME writes landed but one tool failed requeues, but is
    NOT counted unproductive — partial progress must not trip the escalate ladder."""
    from errorta_council.coding.topology import DEV, Assign
    store = LedgerStore("prpartial")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)
    task = store.add_task(title="impl", role=DEV)

    def caller(member, prompt):
        return json.dumps({
            "schema_version": "coding_turn.v1", "role": "dev",
            "task_id": task.task_id,
            "intent": {"kind": "tool_plan", "task_type": "implementation",
                       "tool_calls": [
                           {"tool": "code_write", "args": {"path": "calc.py", "content": _ADD}},
                           {"tool": "MemRead", "args": {"ref": "mem:whatever"}},
                       ]}})

    rt = build_run_turn(store, _real_ws("prpartial", store),
                        members_by_coding_role(MEMBERS), caller, guardrail_enabled=True)
    outcome = rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)

    assert outcome.kind == "noop"
    assert outcome.unproductive is False           # partial progress not penalised
    assert store.list_tasks(state="todo")


def _real_ws(pid, store):
    from errorta_council.coding.workspace import CodingWorkspace
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)
    return ws


def test_members_by_coding_role() -> None:
    by = members_by_coding_role(MEMBERS)
    assert by["pm"][0]["id"] == "m-pm" and by["dev"][0]["id"] == "m-dev"


def test_gateway_member_caller_wraps_async(tmp_path) -> None:
    from errorta_council.coding.runner import gateway_member_caller

    class StubResult:
        content = '{"schema_version": "coding_turn.v1"}'

    class StubGateway:
        async def call(self, req):
            assert req.messages[0]["content"].startswith("hello")
            return StubResult()

    caller = gateway_member_caller(StubGateway())
    out = caller({"id": "m", "gateway_route_id": "r", "provider_kind": "local"}, "hello prompt")
    assert "coding_turn.v1" in out


def test_pm_malformed_turn_fails_closed(tmp_errorta_home: Path) -> None:
    store = LedgerStore("cpmbad")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)

    def caller(member, prompt):
        return "totally not json"

    rt = build_run_turn(store, None, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    outcome = rt(Plan(member_id="m-pm"), store)
    assert outcome.kind == "planned" and outcome.made_progress is False
    assert any(d["choice"] == "pm_turn_rejected" for d in store.list_decisions())
    assert store.list_tasks() == []
    assert store.get_project().status != "done"


def test_interjection_survives_malformed_pm_turn(tmp_errorta_home: Path) -> None:
    store = LedgerStore("cinterjbad")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)
    store.record_interjection("optimize for memory")

    def caller(member, prompt):
        return "not json"

    rt = build_run_turn(store, None, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Plan(member_id="m-pm"), store)
    assert len(store.list_unconsumed_interjections()) == 1


def test_pm_depends_on_resolved_to_task_ids(tmp_errorta_home: Path) -> None:
    store = LedgerStore("cdeps")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)

    def caller(member, prompt):
        return json.dumps({
            "schema_version": "coding_turn.v1", "role": "pm",
            "intent": {"kind": "plan", "done": False, "tasks": [
                {"title": "build it", "role": "dev"},
                {"title": "test it", "role": "tester", "depends_on": ["build it"]},
            ]}})

    rt = build_run_turn(store, None, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Plan(member_id="m-pm"), store)
    tasks = {t.title: t for t in store.list_tasks()}
    assert tasks["test it"].depends_on == [tasks["build it"].task_id]


def test_strict_governance_pm_review_records_pm_transcript(
    tmp_errorta_home: Path,
) -> None:
    store = LedgerStore("cgovpmreview")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)
    governance = GovernanceStore.for_ledger(store)
    governance.update_state(mode="strict", phase="reviewing_brainstorm")
    artifact = governance.append_artifact(
        kind="brainstorm", title="Brainstorm", state="under_review",
    )
    governance.append_review(
        artifact_id=artifact.artifact_id,
        reviewer_member_id="m-rev",
        verdict="approved",
        reviewer_role="reviewer",
    )

    def caller(member, prompt):
        assert member["id"] == "m-pm"
        assert '"role":"pm"' in prompt
        return json.dumps({
            "schema_version": "governance_turn.v1",
            "role": "pm",
            "intent": {
                "kind": "artifact_review",
                "artifact_id": artifact.artifact_id,
                "verdict": "approved",
                "findings": [],
            },
        })

    rt = build_run_turn(store, None, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    outcome = rt(
        GovernanceReview(
            member_id="m-pm",
            artifact_id=artifact.artifact_id,
            reviewer_role="pm",
        ),
        store,
    )

    assert outcome.kind == "governance_progress"
    assert governance.get_artifact(artifact.artifact_id).state == "approved"
    assert governance.load_state().phase == "drafting_spec"
    turn = store.list_turns()[-1]
    assert turn["role"] == "pm"
    assert turn["member_id"] == "m-pm"
    assert turn["task_id"] == artifact.artifact_id


def test_pm_serializes_same_file_dev_tasks(tmp_errorta_home: Path) -> None:
    store = LedgerStore("coverlap")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)

    def caller(member, prompt):
        return _pm_env(tasks=[
            {"title": "update pricing", "role": "dev", "detail": "Change pricing.py"},
            {"title": "cover pricing", "role": "dev", "detail": "Test pricing.py"},
        ])

    rt = build_run_turn(store, None, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Plan(member_id="m-pm"), store)

    tasks = store.list_tasks(role="dev")
    assert len(tasks) == 2
    assert tasks[1].depends_on == [tasks[0].task_id]


class _ConflictWs:
    def __init__(self, *, updated: bool = False) -> None:
        self.updated = updated
        self.calls: list[tuple[str, str, str]] = []

    def pr_diff(self, branch: str) -> str:
        return "diff"

    def update_branch_from_base(self, task_id: str, branch: str, *, base: str = "master"):
        self.calls.append((task_id, branch, base))
        if self.updated:
            return {"updated": True, "conflicts": [], "head": "newhead", "changed": True}
        return {"updated": False, "conflicts": ["pricing.py", "test_pricing.py"],
                "head": "oldhead", "changed": False}


def test_seeded_conflict_pr_redispatches_resolve_task_next_pm_plan(
    tmp_errorta_home: Path,
) -> None:
    store = LedgerStore("cconflict")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)
    task = store.add_task(title="pricing", role="dev", detail="Edit pricing.py")
    store.update_task(task.task_id, state="done")
    pr = store.record_pr(task_id=task.task_id, branch="task-pricing",
                         head="oldhead", dev_member="m-dev")
    store.update_pr(pr["pr_id"], status="conflict",
                    conflicts=["pricing.py", "test_pricing.py"])
    ws = _ConflictWs()

    def caller(member, prompt):
        raise AssertionError("conflict redispatch should not need a PM model turn")

    rt = build_run_turn(store, ws, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    outcome = rt(Plan(member_id="m-pm"), store)

    assert outcome.kind == "planned"
    assert ws.calls == [(task.task_id, "task-pricing", "master")]
    updated = store.get_pr(pr["pr_id"])
    assert updated["status"] == "conflict"
    assert updated["resolve_attempts"] == 1
    resolve = [t for t in store.list_tasks(role="dev")
               if t.title.startswith("resolve conflict:")]
    assert len(resolve) == 1
    assert resolve[0].pr_id == pr["pr_id"]
    assert "pricing.py" in resolve[0].detail
    assert any(d["choice"] == "pr_conflict_redispatched" for d in store.list_decisions())


def test_conflict_redispatch_blocks_after_retry_cap(tmp_errorta_home: Path) -> None:
    store = LedgerStore("cconflictcap")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)
    task = store.add_task(title="pricing", role="dev", detail="Edit pricing.py")
    store.update_task(task.task_id, state="done")
    pr = store.record_pr(task_id=task.task_id, branch="task-pricing",
                         head="oldhead", dev_member="m-dev")
    store.update_pr(pr["pr_id"], status="conflict", conflicts=["pricing.py"],
                    resolve_attempts=2)
    ws = _ConflictWs()

    def caller(member, prompt):
        raise AssertionError("blocked conflict should not need a PM model turn")

    rt = build_run_turn(store, ws, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Plan(member_id="m-pm"), store)

    blocked = store.get_pr(pr["pr_id"])
    assert blocked["status"] == "blocked"
    assert blocked["blocked_reason"] == "conflict resolve retry cap reached"
    assert ws.calls == []
    assert any(d["choice"] == "pr_conflict_blocked" for d in store.list_decisions())


def test_interjection_pins_to_pm_prompt_then_consumed(tmp_errorta_home: Path) -> None:
    store = LedgerStore("cinterj")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)
    store.record_interjection("optimize for memory over speed")
    prompts: list[str] = []

    def caller(member, prompt):
        prompts.append(prompt)
        return _pm_env(tasks=[{"title": "do it", "role": "dev"}])

    rt = build_run_turn(store, None, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Plan(member_id="m-pm"), store)
    assert "optimize for memory over speed" in prompts[0]
    assert "AUTHORITATIVE USER DIRECTION" in prompts[0]


# --- F091: PR supersession -------------------------------------------------

class _WsStub:
    """Minimal workspace stub for the pure-function supersession helpers."""
    def __init__(self, branches=()):
        self._branches = list(branches)
        self.deleted: list[str] = []

    def list_branches(self):
        return list(self._branches)

    def delete_branch(self, b):
        self.deleted.append(b)

    def pr_diff(self, b):
        return "non-empty diff"  # so _reconcile_stale never abandons it


def test_prune_dead_branches_prunes_superseded(tmp_errorta_home: Path) -> None:
    store = LedgerStore("sup-prune")
    store.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    pr = store.record_pr(task_id="t-a", branch="task-t-a", head="h", dev_member="m")
    store.update_pr(pr["pr_id"], status="superseded", superseded_by_pr_id="pr-b")
    ws = _WsStub(branches=["task-t-a", "master"])
    _prune_dead_branches(store, ws)
    assert "task-t-a" in ws.deleted  # superseded branch reclaimed


def test_reconcile_stale_skips_superseded(tmp_errorta_home: Path) -> None:
    store = LedgerStore("sup-recon")
    store.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    pr = store.record_pr(task_id="t-a", branch="task-t-a", head="h", dev_member="m")
    store.update_pr(pr["pr_id"], status="superseded", superseded_by_pr_id="pr-b")
    _reconcile_stale(store, _WsStub(branches=["task-t-a"]))
    # superseded PR is terminal -> reconcile must not touch it
    assert store.get_pr(pr["pr_id"])["status"] == "superseded"
    assert not any(d["choice"] == "pr_superseded"
                   and d.get("pr_id") != pr["pr_id"]
                   for d in store.list_decisions())


def test_revise_task_carries_pr_backlink(tmp_errorta_home: Path) -> None:
    """When a reviewer rejects, the revise task records pr_id (the superseded PR),
    depends_on (the review task), and the branch in its detail."""
    store = LedgerStore("sup-backlink")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)
    store.set_test_commands({"unit": {"argv": [sys.executable, "-c", "pass"],
                                      "cwd": ".", "timeout_seconds": 30}})

    class RejectOnce(FakeGateway):
        def __init__(self):
            super().__init__()
            self.reviews = 0

        def __call__(self, member, prompt):
            if "You are a reviewer" in prompt:
                self.reviews += 1
                if self.reviews == 1:
                    return _rev_env(_task_id(prompt, "reviewer"), _pr_head(prompt),
                                    approved=False,
                                    findings=[{"severity": "major", "path": "calc.py",
                                               "title": "nope"}])
            return super().__call__(member, prompt)

    runner = CodingRunner("sup-backlink", MEMBERS, RejectOnce(), guardrail_enabled=True)
    runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_iterations=10))

    rejected_pr = next(p for p in store.list_prs()
                       if p.get("reviewer_approved") is False)
    revise = [t for t in store.list_tasks() if t.title.startswith("revise:")]
    assert revise, "no revise task created"
    t = revise[0]
    assert t.pr_id == rejected_pr["pr_id"]
    assert t.depends_on  # chained after the review task
    assert rejected_pr["branch"] in t.detail


def _seed_revise(store, *, root_branch, revise_branch, revise_role="dev"):
    """Record an original PR + a revise task (pr_id back-link) + the revise PR,
    mirroring the runtime shape so _supersede_ancestors can walk it."""
    orig_task = store.add_task(title=f"impl {root_branch}", role="dev")
    orig_pr = store.record_pr(task_id=orig_task.task_id, branch=root_branch,
                              head="h", dev_member="m")
    store.update_pr(orig_pr["pr_id"], status="changes_requested")
    rev_task = store.add_task(title=f"revise: {root_branch}", role=revise_role,
                              pr_id=orig_pr["pr_id"], depends_on=[orig_task.task_id])
    rev_pr = store.record_pr(task_id=rev_task.task_id, branch=revise_branch,
                             head="h2", dev_member="m")
    return orig_pr, rev_task, rev_pr


def test_supersede_ancestors_marks_original(tmp_errorta_home: Path) -> None:
    store = LedgerStore("sup-one")
    store.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    orig_pr, _rev_task, rev_pr = _seed_revise(
        store, root_branch="task-A", revise_branch="task-B")
    store.update_pr(rev_pr["pr_id"], status="merged")

    _supersede_ancestors(store, _WsStub(branches=["task-A"]), store.get_pr(rev_pr["pr_id"]))

    a = store.get_pr(orig_pr["pr_id"])
    assert a["status"] == "superseded"
    assert a["superseded_by_pr_id"] == rev_pr["pr_id"]
    assert any(d["choice"] == "pr_superseded" for d in store.list_decisions())
    # core acceptance signal: the PM no longer sees PR-A as open work
    assert "task-A" not in [p["branch"] for p in store.pr_state_summary()["open_prs"]]


def test_supersede_multi_step_chain(tmp_errorta_home: Path) -> None:
    store = LedgerStore("sup-chain")
    store.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    # A rejected -> B (revise of A) rejected -> C (revise of B) merged
    a_task = store.add_task(title="impl A", role="dev")
    a_pr = store.record_pr(task_id=a_task.task_id, branch="task-A", head="h", dev_member="m")
    store.update_pr(a_pr["pr_id"], status="changes_requested")
    b_task = store.add_task(title="revise: task-A", role="dev",
                            pr_id=a_pr["pr_id"], depends_on=[a_task.task_id])
    b_pr = store.record_pr(task_id=b_task.task_id, branch="task-B", head="h2", dev_member="m")
    store.update_pr(b_pr["pr_id"], status="changes_requested")
    c_task = store.add_task(title="revise: task-B", role="dev",
                            pr_id=b_pr["pr_id"], depends_on=[b_task.task_id])
    c_pr = store.record_pr(task_id=c_task.task_id, branch="task-C", head="h3", dev_member="m")
    store.update_pr(c_pr["pr_id"], status="merged")

    _supersede_ancestors(store, _WsStub(), store.get_pr(c_pr["pr_id"]))

    assert store.get_pr(a_pr["pr_id"])["status"] == "superseded"
    assert store.get_pr(b_pr["pr_id"])["status"] == "superseded"
    open_branches = [p["branch"] for p in store.pr_state_summary()["open_prs"]]
    assert "task-A" not in open_branches and "task-B" not in open_branches


def test_supersede_leaves_independent_pr_untouched(tmp_errorta_home: Path) -> None:
    store = LedgerStore("sup-indep")
    store.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    orig_pr, _rev_task, rev_pr = _seed_revise(
        store, root_branch="task-A", revise_branch="task-B")
    store.update_pr(rev_pr["pr_id"], status="merged")
    # an unrelated, never-rejected PR with no back-link
    indep_task = store.add_task(title="impl X", role="dev")
    indep_pr = store.record_pr(task_id=indep_task.task_id, branch="task-X",
                               head="hx", dev_member="m")
    store.update_pr(indep_pr["pr_id"], status="changes_requested")

    _supersede_ancestors(store, _WsStub(), store.get_pr(rev_pr["pr_id"]))

    assert store.get_pr(orig_pr["pr_id"])["status"] == "superseded"
    # the independent PR is NOT swept in (ancestor-chain walk only)
    assert store.get_pr(indep_pr["pr_id"])["status"] == "changes_requested"
