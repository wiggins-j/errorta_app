"""Spec 08 — task-creation dedupe.

The observed failure: 130 todo tasks across ~35 distinct titles, all restating
2–3 real jobs. ``_materialize_pm_tasks`` blind-appended every proposal, so a PM
that narrated the duplication ("47+ duplicate tasks all attempting the same
fix") could then create another one and still register as productive.

Covered here: the gate rejects real duplicates of OPEN tasks, keeps the return
contract honest (``made_progress`` goes False on an all-duplicate batch, which
re-arms pm_idle/NO_PROGRESS), preserves ``depends_on`` resolution through a
rejection — and, most importantly, does NOT collapse distinct jobs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from errorta_council.coding import control_actions as ca
from errorta_council.coding import task_dedupe
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _duplicate_rejection_note,
    _materialize_pm_tasks,
    build_run_turn,
    members_by_coding_role,
)
from errorta_council.coding.schemas import TurnParseError, parse_coding_turn
from errorta_council.coding.topology import DEV, Plan

MEMBER_DICTS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev-1", "enabled": True, "metadata": {"coding_role": DEV}},
]


def _store(tmp_path: Path, name: str = "pdedupe") -> LedgerStore:
    s = LedgerStore(name, root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def _pm_env(tasks: list[dict]) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "pm",
        "intent": {"kind": "plan", "done": False, "tasks": tasks},
    })


def _intent(tasks: list[dict]):
    parsed = parse_coding_turn("pm", None, _pm_env(tasks))
    assert not isinstance(parsed, TurnParseError), parsed
    return parsed.intent


def _plan(store: LedgerStore, tasks: list[dict]) -> list:
    """Drive the real choke point and return only the genuinely created tasks."""
    return _materialize_pm_tasks(store, _intent(tasks))


def _skips(store: LedgerStore) -> list[dict]:
    return [d for d in store.list_decisions()
            if d.get("choice") == "duplicate_task_rejected"]


# --- the duplicate test itself ------------------------------------------------

def test_byte_identical_titles_in_one_plan_create_once(tmp_path: Path) -> None:
    s = _store(tmp_path, "d1")
    created = _plan(s, [
        {"title": "Fix the acceptance test harness", "role": "dev",
         "detail": "Acceptance: the harness runs."},
        {"title": "Fix the acceptance test harness", "role": "dev",
         "detail": "Acceptance: the harness runs."},
    ])

    assert len(created) == 1
    assert len(s.list_tasks(role=DEV)) == 1
    skips = _skips(s)
    assert len(skips) == 1
    assert skips[0]["matched_task_id"] == created[0].task_id
    assert skips[0]["rule"] == task_dedupe.RULE_TITLE
    assert skips[0]["planned_title"] == "Fix the acceptance test harness"


def test_filler_verb_variant_is_rejected(tmp_path: Path) -> None:
    """"Fix X" / "Create X" / "Consolidate X" is the exact restatement pattern
    the field run produced 35 times."""
    s = _store(tmp_path, "d2")
    s.add_task(title="Fix the renderer init interface", role=DEV)

    created = _plan(s, [
        {"title": "Consolidate the renderer init interface", "role": "dev",
         "detail": "Acceptance: renderer.init has one interface."},
    ])

    assert created == []
    assert _skips(s)[0]["rule"] == task_dedupe.RULE_TITLE


def test_reworded_title_on_the_same_file_is_allowed(tmp_path: Path) -> None:
    """FIX 2: rule (b)'s title floor is now 0.8 (was 0.6). A same-file pair whose
    titles only reach Jaccard 0.67 is NOT strong enough to dedupe — a shared file
    is not evidence of a duplicate. Per the module docstring, tolerating a possible
    duplicate is acceptable; dropping real work is not. So this reworded same-file
    pair is now allowed through as two tasks."""
    s = _store(tmp_path, "d2b")
    s.add_task(title="Fix the renderer init interface", role=DEV,
               detail="Edit src/renderer.ts")

    created = _plan(s, [
        # title Jaccard 0.67 — under the 0.8 bar rule (b) now shares with rule (a).
        {"title": "Repair the renderer init interface wiring", "role": "dev",
         "detail": "Acceptance: wiring done. Edit src/renderer.ts"},
    ])

    assert len(created) == 1
    assert _skips(s) == []


# --- the false-positive guards (the real risk) --------------------------------

def test_same_title_different_target_paths_both_created(tmp_path: Path) -> None:
    """THE non-collapse case. Two tasks doing the same *kind* of work on two
    different files are two jobs. Naming different files vetoes the title rule."""
    s = _store(tmp_path, "d3")
    created = _plan(s, [
        {"title": "Add the missing type annotations", "role": "dev",
         "detail": "Acceptance: annotated. In scope: src/parser.py"},
        {"title": "Add the missing type annotations", "role": "dev",
         "detail": "Acceptance: annotated. In scope: src/emitter.py"},
    ])

    assert len(created) == 2
    assert {t.task_id for t in created} == {t.task_id for t in s.list_tasks(role=DEV)}
    assert _skips(s) == []


def test_distinct_jobs_on_one_shared_file_both_created(tmp_path: Path) -> None:
    """Same file, same role, unrelated titles — implement vs. document is not one
    job. Rule (b) must not fire on the path signal alone."""
    s = _store(tmp_path, "d4")
    created = _plan(s, [
        {"title": "update pricing", "role": "dev", "detail": "Change pricing.py"},
        {"title": "document pricing", "role": "dev", "detail": "Doc pricing.py"},
    ])

    assert len(created) == 2
    assert _skips(s) == []


def test_pagination_vs_sorting_same_endpoint_both_created(tmp_path: Path) -> None:
    """FIX 2 regression: two distinct jobs on users.py — title Jaccard 0.667, under
    the raised 0.8 rule (b) floor. The old 0.6 floor wrongly collapsed them."""
    s = _store(tmp_path, "fp1")
    created = _plan(s, [
        {"title": "Add pagination to the users endpoint", "role": "dev",
         "detail": "Acceptance: paginated. In scope: src/users.py"},
        {"title": "Add sorting to the users endpoint", "role": "dev",
         "detail": "Acceptance: sorted. In scope: src/users.py"},
    ])
    assert len(created) == 2
    assert _skips(s) == []


def test_unit_vs_integration_tests_same_file_both_created(tmp_path: Path) -> None:
    """FIX 2 regression: "unit tests" vs "integration tests" on parser.py — title
    Jaccard 0.60, below the 0.8 floor. The old 0.6 floor wrongly collapsed them."""
    s = _store(tmp_path, "fp2")
    created = _plan(s, [
        {"title": "Add unit tests for parser", "role": "dev",
         "detail": "Acceptance: unit covered. In scope: src/parser.py"},
        {"title": "Add integration tests for parser", "role": "dev",
         "detail": "Acceptance: integration covered. In scope: src/parser.py"},
    ])
    assert len(created) == 2
    assert _skips(s) == []


def test_combat_level_50_vs_60_both_created(tmp_path: Path) -> None:
    """FIX 2 numeric veto: a level number is a load-bearing distinguisher, so
    "level 50" and "level 60" on combat.py are two jobs regardless of Jaccard."""
    s = _store(tmp_path, "fp3")
    created = _plan(s, [
        {"title": "Fix combat at level 50", "role": "dev",
         "detail": "Acceptance: lvl 50 ok. In scope: src/combat.py"},
        {"title": "Fix combat at level 60", "role": "dev",
         "detail": "Acceptance: lvl 60 ok. In scope: src/combat.py"},
    ])
    assert len(created) == 2
    assert _skips(s) == []


def test_parser_python_3_11_vs_3_12_both_created(tmp_path: Path) -> None:
    """FIX 2 numeric veto: a version number distinguishes the jobs, so "python
    3.11" and "python 3.12" on parser.py are two tasks, not one."""
    s = _store(tmp_path, "fp4")
    created = _plan(s, [
        {"title": "Update parser for python 3.11", "role": "dev",
         "detail": "Acceptance: 3.11 ok. In scope: src/parser.py"},
        {"title": "Update parser for python 3.12", "role": "dev",
         "detail": "Acceptance: 3.12 ok. In scope: src/parser.py"},
    ])
    assert len(created) == 2
    assert _skips(s) == []


def test_duplicate_of_a_done_task_is_created(tmp_path: Path) -> None:
    """Re-doing finished work is legitimate (a regression). Only OPEN tasks
    suppress a create."""
    s = _store(tmp_path, "d5")
    finished = s.add_task(title="Fix the acceptance test harness", role=DEV)
    s.update_task(finished.task_id, state="done")

    created = _plan(s, [
        {"title": "Fix the acceptance test harness", "role": "dev",
         "detail": "Acceptance: the harness runs again."},
    ])

    assert len(created) == 1
    assert created[0].task_id != finished.task_id
    assert _skips(s) == []


def test_duplicate_of_a_dropped_task_is_created(tmp_path: Path) -> None:
    s = _store(tmp_path, "d6")
    abandoned = s.add_task(title="Wire the settings panel", role=DEV)
    s.update_task(abandoned.task_id, state="dropped")

    created = _plan(s, [{"title": "Wire the settings panel", "role": "dev",
                         "detail": "Acceptance: panel wired."}])

    assert len(created) == 1


# --- the return contract (re-arms pm_idle / NO_PROGRESS) ----------------------

def test_all_duplicate_batch_reports_no_progress(tmp_path: Path) -> None:
    """The highest-leverage consequence: a batch of nothing but duplicates must
    NOT read as progress, or a churning PM looks productive forever."""
    s = _store(tmp_path, "d7")
    s.add_task(title="Fix the acceptance test harness", role=DEV)
    s.add_task(title="Consolidate the duplicate tasks", role=DEV)

    def caller(_member, _prompt):
        return _pm_env([
            {"title": "Create the acceptance test harness", "role": "dev",
             "detail": "Acceptance: the harness runs."},
            {"title": "Fix the duplicate tasks", "role": "dev",
             "detail": "Acceptance: no duplicates."},
        ])

    run_turn = build_run_turn(s, None, members_by_coding_role(MEMBER_DICTS),
                              caller, guardrail_enabled=True)
    outcome = run_turn(Plan(member_id="m-pm"), s)

    assert outcome.kind == "planned"
    assert outcome.made_progress is False
    assert len(s.list_tasks(role=DEV)) == 2  # nothing new landed
    assert len(_skips(s)) == 2


def test_partially_duplicate_batch_still_reports_progress(tmp_path: Path) -> None:
    s = _store(tmp_path, "d8")
    s.add_task(title="Fix the acceptance test harness", role=DEV)

    def caller(_member, _prompt):
        return _pm_env([
            {"title": "Create the acceptance test harness", "role": "dev",
             "detail": "Acceptance: the harness runs."},
            {"title": "Publish the nightly build report", "role": "dev",
             "detail": "Acceptance: report published."},
        ])

    run_turn = build_run_turn(s, None, members_by_coding_role(MEMBER_DICTS),
                              caller, guardrail_enabled=True)
    outcome = run_turn(Plan(member_id="m-pm"), s)

    assert outcome.made_progress is True
    assert len(s.list_tasks(role=DEV)) == 2


# --- dependency resolution survives a rejection -------------------------------

def test_rejected_title_resolves_to_the_matched_task_in_depends_on(
        tmp_path: Path) -> None:
    """A sibling naming the rejected task by title must end up depending on the
    task that already does that job — not on a dangling string."""
    s = _store(tmp_path, "d9")
    existing = s.add_task(title="Create the auth handler", role=DEV)

    created = _plan(s, [
        {"title": "Fix the auth handler", "role": "dev",
         "detail": "Acceptance: auth handler works."},
        {"title": "Wire the login page", "role": "dev",
         "detail": "Acceptance: login page wired.",
         "depends_on": ["Fix the auth handler"]},
    ])

    assert len(created) == 1
    login = next(t for t in s.list_tasks(role=DEV) if t.title == "Wire the login page")
    assert login.depends_on == [existing.task_id]


# --- the PM is told the truth -------------------------------------------------

def test_pm_note_names_the_rejections_and_clears_when_matched_task_closes(
        tmp_path: Path) -> None:
    s = _store(tmp_path, "d10")
    existing = s.add_task(title="Fix the acceptance test harness", role=DEV)
    _plan(s, [{"title": "Create the acceptance test harness", "role": "dev",
               "detail": "Acceptance: the harness runs."}])

    note = _duplicate_rejection_note(s)
    assert existing.task_id in note
    assert "do not re-propose" in note.lower()
    assert "Create the acceptance test harness" in note

    # Once the real task is finished the nag is settled history — it must clear.
    s.update_task(existing.task_id, state="done")
    assert _duplicate_rejection_note(s) == ""


def test_pm_note_is_empty_without_rejections(tmp_path: Path) -> None:
    assert _duplicate_rejection_note(_store(tmp_path, "d11")) == ""


# --- the re-scope path must not dedupe against the task it replaces -----------

def test_rescope_replacement_is_not_deduped_against_its_own_parent(
        tmp_path: Path) -> None:
    """PMAssist re-scopes a stuck task into smaller pieces, which restate its
    job by design — and the parent is still open at materialize time (it is
    dropped moments later). Deduping there would wedge the stuck task forever."""
    s = _store(tmp_path, "d17")
    stuck = s.add_task(title="Fix the acceptance test harness", role=DEV)
    s.update_task(stuck.task_id, state="doing")

    created = _materialize_pm_tasks(
        s, _intent([{"title": "Fix the acceptance test harness", "role": "dev",
                     "detail": "Acceptance: harness runs under Node."}]),
        parent_task=next(t for t in s.list_tasks() if t.task_id == stuck.task_id))

    assert len(created) == 1
    assert _skips(s) == []


# --- second choke point: control_actions.create_task --------------------------

def test_control_action_create_task_refuses_a_duplicate(tmp_path: Path) -> None:
    s = _store(tmp_path, "d12")
    existing = s.add_task(title="Fix the font crash", role=DEV)

    with pytest.raises(ca.ControlActionError) as exc:
        ca.create_task(s, title="Consolidate the font crash", role="dev")

    assert exc.value.code == "duplicate_task"
    assert exc.value.extra["matched_task_id"] == existing.task_id
    assert len(s.list_tasks(role=DEV)) == 1


def test_control_action_refusal_surfaces_through_apply_actions(
        tmp_path: Path) -> None:
    s = _store(tmp_path, "d13")
    s.add_task(title="Fix the font crash", role=DEV)

    applied, refusals = ca.apply_actions(
        s, [{"type": "create_task", "title": "Fix the font crash"}], available=[])

    assert applied == []
    assert len(refusals) == 1
    assert refusals[0]["code"] == "duplicate_task"


def test_control_action_still_creates_a_genuinely_new_task(tmp_path: Path) -> None:
    s = _store(tmp_path, "d14")
    s.add_task(title="Fix the font crash", role=DEV)

    change = ca.create_task(s, title="Add a dark-mode toggle", role="dev")

    assert change.restore_target == "task"
    assert len(s.list_tasks(role=DEV)) == 2


def test_control_action_declared_target_files_disambiguate(tmp_path: Path) -> None:
    """Two identically-titled tasks that declare different files are two jobs."""
    s = _store(tmp_path, "d15")
    ca.create_task(s, title="Add type annotations", role="dev",
                   target_files=["src/parser.py"])
    ca.create_task(s, title="Add type annotations", role="dev",
                   target_files=["src/emitter.py"])

    assert len(s.list_tasks(role=DEV)) == 2


# --- the pure predicate -------------------------------------------------------

def test_normalized_tokens_drops_leading_filler_only() -> None:
    assert task_dedupe.normalized_tokens("Fix the parser!") == frozenset(
        {"the", "parser"})
    # a filler verb in a non-leading position is real content
    assert "fix" in task_dedupe.normalized_tokens("Ship the hot fix")
    # an all-filler title keeps its tokens rather than normalizing to empty
    assert task_dedupe.normalized_tokens("Fix") == frozenset({"fix"})
    assert task_dedupe.normalized_tokens("Fix") != task_dedupe.normalized_tokens(
        "Update")


def test_find_duplicate_threshold_is_conservative() -> None:
    index = [task_dedupe.index_entry(
        task_id="t-1", title="add the missing type annotations to the parser",
        role=DEV, paths=[])]
    # one token differs out of six -> 5/7 = 0.71, under the 0.8 bar
    assert task_dedupe.find_duplicate(
        index, title="add the missing type annotations to the emitter",
        role=DEV, paths=[]) is None
    assert task_dedupe.find_duplicate(
        index, title="add the missing type annotations to the parser",
        role=DEV, paths=[]) is not None


def test_find_duplicate_requires_the_same_role() -> None:
    index = [task_dedupe.index_entry(task_id="t-1", title="check the build",
                                     role="reviewer", paths=["ci.yml"])]
    # rule (b) is role-scoped; the titles are unrelated so rule (a) cannot fire
    assert task_dedupe.find_duplicate(
        index, title="rerun the build", role=DEV, paths=["ci.yml"]) is None


def test_build_open_index_keeps_only_open_tasks(tmp_path: Path) -> None:
    s = _store(tmp_path, "d16")
    todo = s.add_task(title="a", role=DEV)
    doing = s.add_task(title="b", role=DEV)
    done = s.add_task(title="c", role=DEV)
    blocked = s.add_task(title="d", role=DEV)
    s.update_task(doing.task_id, state="doing")
    s.update_task(done.task_id, state="done")
    s.update_task(blocked.task_id, state="blocked")

    index = task_dedupe.build_open_index(s.list_tasks())
    assert {e.task_id for e in index} == {todo.task_id, doing.task_id}
