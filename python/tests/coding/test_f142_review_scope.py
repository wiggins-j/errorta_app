"""F142 Slice 1 (WS-A) — the reviewer judges a PR against ITS OWN task's scope,
not the whole North Star / Definition of Done.

Locks the acceptance criteria in `docs/specs/F142-task-scoped-pr-review.md`:

- AC1 (prompt content): `_review_pr_prompt` states the task's title/detail as
  the scope of THIS PR, frames it as "ONE scoped task", and drops the old
  "moves ... toward the Definition of Done and the full required API" wording
  that made a correct foundation slice fail review for "not being the whole
  product yet".
- AC2 (decision framing): the prompt EXPLICITLY tells the reviewer that overall
  product incompleteness / other tasks' functionality being absent is not
  grounds for rejection (a real model call is out of scope for a unit test — we
  assert on the prompt wording).
- Governance: a governance-sourced task's prompt points the acceptance bar at
  the slice `done_when` / `review_focus`.
"""
import json

from errorta_council.coding.ledger import Task
from errorta_council.coding.runner import _REVIEW_DIFF_CAP, _review_pr_prompt

_PR = {"pr_id": "pr-1", "branch": "task/t-1", "head": "abc123", "task_id": "t-1"}
_DIFF = "diff --git a/game.py b/game.py\n+class Move:\n+    pass\n"


def _legacy_task() -> Task:
    return Task(
        task_id="t-1",
        title="Create game.py with constants, Move class, and Creature class",
        role="dev",
        detail="Define the module constants, a Move dataclass, and a Creature "
               "class in a single file named game.py.",
    )


def _governance_task() -> Task:
    return Task(
        task_id="t-1",
        title="Foundation slice",
        role="dev",
        detail="foundation detail",
        source_plan_artifact_id="plan-1",
        source_slice_id="slice-1",
    )


# --- AC1: prompt content ----------------------------------------------------

def test_ac1_prompt_contains_task_scope_and_one_scoped_task_framing() -> None:
    task = _legacy_task()
    prompt = _review_pr_prompt(task, _PR, _DIFF, project_context="")
    # The task's own title AND detail appear as the scope of THIS PR.
    assert task.title in prompt
    assert task.detail in prompt
    # The reviewer is told this is ONE scoped task, not the whole product.
    assert "ONE scoped task" in prompt
    assert "scope of THIS PR" in prompt


def test_ac1_prompt_drops_north_star_completion_bar_wording() -> None:
    prompt = _review_pr_prompt(_legacy_task(), _PR, _DIFF, project_context="")
    # The old wording that made the reviewer grade against the whole product.
    assert "moves the project toward the Definition of Done and the full "\
           "required API" not in prompt
    # And the old "judge the project as it would be AFTER this merges" push.
    assert "Judge the project as it would be AFTER this merges" not in prompt


# --- AC2: decision framing --------------------------------------------------

def test_ac2_prompt_says_incompleteness_is_not_grounds_for_rejection() -> None:
    prompt = _review_pr_prompt(_legacy_task(), _PR, _DIFF, project_context="")
    # Overall-incompleteness / OTHER tasks' functionality absent is an explicit
    # NON-reason to request changes.
    assert "NOT a reason to request changes: the overall product being "\
           "incomplete, or functionality that belongs to OTHER tasks being "\
           "absent" in prompt
    # Sibling / future work is framed as out-of-scope, not a defect.
    assert "out-of-scope future work" in prompt


def test_ac2_prompt_keeps_cross_project_harm_as_blocking() -> None:
    """Clause (b) must survive: break/drop/contract-mismatch stays blocking so
    F139 WS-B / WS-D2 still fire — and the reviewer is told it MUST write a
    finding naming the mismatch (that's the only trigger for WS-D2)."""
    prompt = _review_pr_prompt(_legacy_task(), _PR, _DIFF, project_context="")
    assert "breaks or drops any code already on master" in prompt
    assert "contract mismatch" in prompt
    assert "MUST write a finding naming it" in prompt


def test_ac2_prompt_treats_partial_in_scope_implementation_as_a_defect() -> None:
    """Finding B4 regression lock: 'incompleteness is fine' must NOT leak into
    THIS task's own multi-part scope — a partial implementation of the task's own
    stated scope is a blocking defect, distinct from another task's work missing."""
    prompt = _review_pr_prompt(_legacy_task(), _PR, _DIFF, project_context="")
    assert "partial or incorrect implementation of THIS task" in prompt
    assert "missing part of THIS task's scope" in prompt


def test_ac2_approved_false_still_requires_a_finding() -> None:
    prompt = _review_pr_prompt(_legacy_task(), _PR, _DIFF, project_context="")
    assert "If approved=false you MUST include at least one finding." in prompt


def test_truncated_diff_fails_closed_instead_of_approving_unseen_code() -> None:
    diff = "x" * _REVIEW_DIFF_CAP + "UNSEEN_TAIL"
    prompt = _review_pr_prompt(_legacy_task(), _PR, diff, project_context="")
    assert "UNSEEN_TAIL" not in prompt
    assert "review coverage is incomplete" in prompt
    assert "unseen code cannot be approved" in prompt
    assert '"approved": false' in prompt
    assert "split or reduce" in prompt
    example = prompt.split("Reply with ONLY a coding_turn.v1 envelope: ", 1)[1]
    example = example.split(". If approved=false", 1)[0]
    assert json.loads(example)["intent"]["approved"] is False


def test_complete_diff_keeps_normal_approval_example() -> None:
    prompt = _review_pr_prompt(_legacy_task(), _PR, _DIFF, project_context="")
    assert "review coverage is incomplete" not in prompt
    assert '"approved": true' in prompt
    example = prompt.split("Reply with ONLY a coding_turn.v1 envelope: ", 1)[1]
    example = example.split(". If approved=false", 1)[0]
    assert json.loads(example)["intent"]["approved"] is True


# --- governance-sourced acceptance bar --------------------------------------

def test_governance_task_points_acceptance_bar_at_done_when() -> None:
    prompt = _review_pr_prompt(_governance_task(), _PR, _DIFF, project_context="")
    # The governance branch is exercised and points at the slice done_when /
    # review_focus injected via the governance planning context.
    assert "governance-sourced" in prompt
    assert "done_when" in prompt


def test_legacy_task_points_acceptance_bar_at_task_scope() -> None:
    prompt = _review_pr_prompt(_legacy_task(), _PR, _DIFF, project_context="")
    # A non-governance task does NOT claim governance provenance.
    assert "governance-sourced" not in prompt
    assert "acceptance bar is the task scope" in prompt
