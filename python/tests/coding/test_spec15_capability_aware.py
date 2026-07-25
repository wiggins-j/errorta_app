"""Spec 15 — capability-aware planning and finding routing.

Item 1: the role-capability manifest, derived from `_ROLE_TOOLS` + policy flags.
Item 2: the execution-imperative classifier (the table below IS the spec of the lint).
"""
from __future__ import annotations

import pytest

from errorta_council.coding import capabilities
from errorta_council.coding.topology import DEV, PM, REVIEWER, TESTER

# --------------------------------------------------------------------------- #
# Item 2 — the classifier table.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("title, expected", [
    ("Run acceptance gate and fix failures", "execution"),
    ("Measure and report frame time", "execution"),
    ("Reproduce the crash and paste the stack", "execution"),
    ("Write an acceptance test for level triviality", "authoring"),
    ("Add a benchmark harness", "authoring"),
    ("Create a test that fails on trivial levels", "authoring"),
    ("write a script that runs the levels", "authoring"),
    ("Run the linter", "other"),           # run-verb, no evidence demand
    ("Implement the gravity solver", "other"),
    ("Fix the black-screen bug in Render.init", "other"),
    # Regression locks (code-review finding 1): ordinary app-building vocabulary
    # must NOT classify as execution just because it contains launch/profile/results.
    ("Add a launch screen and a results table", "other"),
    ("Add a profile page that reports user metrics", "other"),
    ("Detect the runtime profile and report it", "other"),
    ("Build the results dashboard with a metrics table", "other"),
])
def test_classify_task_text_table(title, expected) -> None:
    assert capabilities.classify_task_text(title) == expected


def test_runtime_is_not_a_run_verb() -> None:
    # 'runtime' must not trip \brun\b — Spec 13 is all about runtime profiles — and
    # 'profile' is no longer a run-verb, so a runtime task is never execution.
    assert capabilities.classify_task_text(
        "Detect the runtime profile and report it") == "other"
    assert capabilities.classify_task_text("Register the runtime profile") == "other"


def test_authoring_beats_a_benchmark_run_verb() -> None:
    # 'benchmark' is both an authoring noun and a run-verb; authoring wins.
    assert capabilities.classify_task_text("Add a benchmark harness") == "authoring"


# --------------------------------------------------------------------------- #
# Item 1 — the manifest is derived, not authored.
# --------------------------------------------------------------------------- #

class _FakeStore:
    project_id = "p"


class _Policy:
    def __init__(self, dev_repo_read=False, reviewer_repo_read=False):
        self.dev_repo_read = dev_repo_read
        self.reviewer_repo_read = reviewer_repo_read


def test_manifest_reflects_role_tools(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.gate_state, "gate_available", lambda _s: False)
    man = capabilities.capability_manifest(_FakeStore(), _Policy())
    assert man[DEV].tools == ("code_write",)
    assert man[REVIEWER].tools == ()
    assert man[TESTER].tools == ()
    assert man[PM].tools == ()
    # No role can run a command from inside a turn.
    assert all(not man[r].can_execute for r in (PM, DEV, REVIEWER, TESTER))


def test_manifest_tracks_policy_and_gate_flags(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.gate_state, "gate_available", lambda _s: True)
    man = capabilities.capability_manifest(
        _FakeStore(), _Policy(dev_repo_read=True, reviewer_repo_read=True))
    assert man[DEV].repo_read is True
    assert man[REVIEWER].repo_read is True
    assert man[TESTER].repo_read is False
    assert man[DEV].gate_available is True

    off = capabilities.capability_manifest(_FakeStore(), _Policy())
    assert off[DEV].repo_read is False


def test_pm_segment_names_the_gate_rule(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.gate_state, "gate_available", lambda _s: False)
    seg = capabilities.pm_capability_segment(_FakeStore(), _Policy())
    assert "No role can run a command" in seg
    assert "acceptance gate" in seg
    assert "dev" in seg and "code_write" in seg


# --------------------------------------------------------------------------- #
# Item 2 — the lint at the PM task-materialization chokepoint.
# --------------------------------------------------------------------------- #

def _planned(title: str, detail: str = ""):
    from types import SimpleNamespace
    return SimpleNamespace(
        title=title, detail=detail, task_type="implementation",
        difficulty_tier="mid", preferred_member_id=None, preferred_route_id=None,
        assignment_rationale=None, depends_on=[])


def _intent(*planned):
    from types import SimpleNamespace
    return SimpleNamespace(tasks=list(planned))


def _real_store(pid, tmp_path):
    from errorta_council.coding.ledger import LedgerStore
    s = LedgerStore(pid, root=tmp_path / f"l-{pid}")
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def test_execution_task_refused_without_a_gate(tmp_errorta_home, tmp_path) -> None:
    from errorta_council.coding import runner
    s = _real_store("cap1", tmp_path)
    tasks = runner._materialize_pm_tasks(
        s, _intent(_planned("Run acceptance gate and fix failures")))
    assert tasks == []  # not created — no role can discharge it, no gate exists
    assert any(d["choice"] == "task_requires_absent_capability"
               for d in s.list_decisions())
    # And the refusal surfaces to the PM's next plan turn.
    assert "refused" in runner._capability_refusal_note(s).lower()


def test_execution_task_rewritten_with_a_gate(tmp_errorta_home, tmp_path) -> None:
    from errorta_council.coding import runner
    s = _real_store("cap2", tmp_path)
    s.set_test_commands({"u": {"argv": ["true"], "timeout_seconds": 5}})  # a gate exists
    tasks = runner._materialize_pm_tasks(
        s, _intent(_planned("Run acceptance gate and fix failures")))
    assert len(tasks) == 1
    assert tasks[0].title == capabilities.GATE_FIX_TITLE  # rewritten, not refused
    assert any(d["choice"] == "task_routed_to_gate" for d in s.list_decisions())


def test_authoring_and_ordinary_tasks_are_untouched(tmp_errorta_home, tmp_path) -> None:
    from errorta_council.coding import runner
    s = _real_store("cap3", tmp_path)
    tasks = runner._materialize_pm_tasks(s, _intent(
        _planned("Write an acceptance test for trivial levels"),
        _planned("Implement the gravity solver")))
    titles = {t.title for t in tasks}
    assert "Write an acceptance test for trivial levels" in titles  # authoring
    assert "Implement the gravity solver" in titles                 # other
    assert not any(d["choice"] in ("task_routed_to_gate",
                                   "task_requires_absent_capability")
                   for d in s.list_decisions())


def test_control_action_refuses_execution_task_without_a_gate(
        tmp_errorta_home, tmp_path) -> None:
    from errorta_council.coding import control_actions
    s = _real_store("cap7", tmp_path)
    with pytest.raises(control_actions.ControlActionError):
        control_actions.create_task(
            s, title="Run the acceptance gate and report the results", role="dev")


# --------------------------------------------------------------------------- #
# Item 3 — the same lint on reviewer findings (suppress the spiral-driving revise).
# --------------------------------------------------------------------------- #

def _ws(pid, store):
    from errorta_council.coding.workspace import CodingWorkspace
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)
    return ws


def _pr_and_review(s, ws):
    dev_task = s.add_task(title="add module", role="dev")
    branch = ws.start_task_branch(dev_task.task_id)
    ws.write_file("src/mod.js", "export const x = 1\n", task_id=dev_task.task_id)
    pr = s.record_pr(task_id=dev_task.task_id, branch=branch,
                     head=ws.branch_head(branch), dev_member="m-dev")
    review = s.add_task(title=f"review PR: {dev_task.title}", role="reviewer",
                        pr_id=pr["pr_id"], depends_on=[dev_task.task_id])
    return pr, review


def _reject(s, ws, findings):
    from errorta_council.coding import runner
    pr, review = _pr_and_review(s, ws)
    runner._handle_review_rejection(
        s, ws, pr=pr, task=review, findings=findings, source="reviewer")
    return pr


def test_execution_demand_rejection_spawns_no_revise(
        tmp_errorta_home, tmp_path) -> None:
    s = _real_store("cap5", tmp_path)
    ws = _ws("cap5", s)
    pr = _reject(s, ws, [{"severity": "blocking", "blocking": True,
                          "title": "no evidence the tests were run",
                          "body": "there is no strokes-per-level table"}])
    tasks = s.list_tasks()
    assert not any(t.title.startswith("revise:") for t in tasks)  # spiral suppressed
    assert any(t.role == "pm" and t.title.startswith("unexecutable rejection:")
               for t in tasks)  # routed to the PM instead
    assert any(d["choice"] in ("finding_routed_to_gate",
                               "finding_requires_absent_capability")
               for d in s.list_decisions())
    # And the PR is still changes_requested — never auto-mergeable.
    assert s.get_pr(pr["pr_id"])["status"] == "changes_requested"


def test_all_uncited_rejection_spawns_no_revise(tmp_errorta_home, tmp_path) -> None:
    s = _real_store("cap6", tmp_path)
    ws = _ws("cap6", s)
    _reject(s, ws, [{"severity": "blocking", "blocking": True,
                     "title": "this looks wrong", "body": "", "cited": False}])
    assert not any(t.title.startswith("revise:") for t in s.list_tasks())


def test_a_real_cited_defect_still_spawns_a_revise(
        tmp_errorta_home, tmp_path) -> None:
    s = _real_store("cap8", tmp_path)
    ws = _ws("cap8", s)
    _reject(s, ws, [{"severity": "blocking", "blocking": True,
                     "title": "null deref in init", "body": "src/mod.js:1 crashes",
                     "cited": True, "path": "src/mod.js"}])
    # A genuine, citable code finding is DEV-actionable -> today's behaviour.
    assert any(t.title.startswith("revise:") for t in s.list_tasks())


_EXEC_FINDING = [{"severity": "blocking", "blocking": True,
                  "title": "no evidence the tests were run", "body": ""}]


def test_gate_present_execution_demand_requeues_one_re_review(
        tmp_errorta_home, tmp_path) -> None:
    from errorta_council.coding import runner
    s = _real_store("cap9", tmp_path)
    ws = _ws("cap9", s)
    s.set_test_commands({"u": {"argv": ["true"], "timeout_seconds": 5}})  # a gate exists
    pr, review = _pr_and_review(s, ws)
    runner._handle_review_rejection(s, ws, pr=pr, task=review,
                                    findings=_EXEC_FINDING, source="reviewer")
    tasks = s.list_tasks()
    assert not any(t.title.startswith("revise:") for t in tasks)          # no DEV revise
    assert any(t.role == "reviewer" and "re-review" in t.title
               for t in tasks)                                            # one re-review
    assert not any(t.role == "pm" and t.title.startswith("unexecutable")
                   for t in tasks)                                        # not escalated yet
    assert any(d["choice"] == "review_requeued_for_gate"
               for d in s.list_decisions())


def test_second_unactionable_rejection_on_same_head_escalates(
        tmp_errorta_home, tmp_path) -> None:
    from errorta_council.coding import runner
    s = _real_store("cap10", tmp_path)
    ws = _ws("cap10", s)
    s.set_test_commands({"u": {"argv": ["true"], "timeout_seconds": 5}})
    pr, review1 = _pr_and_review(s, ws)
    runner._handle_review_rejection(s, ws, pr=pr, task=review1,
                                    findings=_EXEC_FINDING, source="reviewer")  # -> re-review
    review2 = s.add_task(title="review PR: recheck", role="reviewer",
                         pr_id=pr["pr_id"], depends_on=[review1.task_id])
    runner._handle_review_rejection(s, ws, pr=pr, task=review2,
                                    findings=_EXEC_FINDING, source="reviewer")  # -> escalate
    # Same head, second time: no second re-review, escalate to the PM instead.
    reviews = [t for t in s.list_tasks()
               if t.role == "reviewer" and "re-review" in t.title]
    assert len(reviews) == 1
    assert any(t.role == "pm" and t.title.startswith("unexecutable")
               for t in s.list_tasks())
