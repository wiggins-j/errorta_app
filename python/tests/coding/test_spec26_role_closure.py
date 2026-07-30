"""SPEC-26 — role capability closure (grant, unseat, or override).

"Grant or delete" is a principle this codebase states, documents, tests, and did
not enforce. ``capabilities.audit_grant_or_delete`` computed it,
``topology_audit`` scored every seated role against it, and
``_audit_topology_advisory`` wrote a decision, raised a non-blocking Alert, and
returned — so the advisory fired on every run and never resolved, and the run
proceeded exactly as if the audit had not run.

This suite locks the four slices that close it:

* **S1** ``role_closure`` — three outcomes (``capable``/``deferred``/
  ``unclosable``), because a boolean cannot distinguish "this REVIEWER will never
  be grounded without an operator edit" from "this TESTER has no dispatchable
  command YET". The split is not severity; it is *who can still act*.
* **S2** the consequence at seat time — **unseat, never refuse**. A refusal would
  refuse the product's own shipped defaults on every new project.
* **S3** re-evaluation on the capability the duty actually needs, plus the
  resolution that has never happened in this codebase.
* **S4** the coupling that makes unseating SAFE: the tester-spawn and merge-gate
  predicates read the same "can a TESTER be dispatched" fact the audit does.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from errorta_council.coding import attention, capabilities
from errorta_council.coding.autonomy import (
    CodingAutonomyPolicy,
    decide_next,
    effective_parallelism,
    policy_from_dict,
    policy_to_dict,
    run_coding_loop,
)
from errorta_council.coding.capabilities import (
    CAPABLE,
    CLOSURE_TABLE,
    DEFERRED,
    UNCLOSABLE,
    capability_manifest,
    capability_override_roles,
    role_closure,
)
from errorta_council.coding.capabilities import (
    # Aliased: pytest would otherwise COLLECT an imported `tester_*` name as a test
    # and fail on its `store` parameter as a missing fixture.
    tester_dispatchable as _tester_dispatchable,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _apply_role_closure,
    _arm_gate_after_merge,
    _reevaluate_role_closure,
    build_run_turn,
    members_by_coding_role,
)
from errorta_council.coding.topology import DEV, PM, REVIEWER, TESTER, Assign
from errorta_council.coding.workspace import CodingWorkspace

_UNIT = {"argv": ["python", "-c", "import sys; sys.exit(0)"], "timeout_seconds": 30,
         "scope": "unit"}
_ACCEPTANCE = {"argv": ["python", "-c", "import sys; sys.exit(0)"],
               "timeout_seconds": 30, "scope": "acceptance"}

_MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]


def _store(pid: str, tmp_path: Path) -> LedgerStore:
    s = LedgerStore(pid, root=tmp_path / f"ledger-{pid}")
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def _ws(pid: str, store: LedgerStore) -> CodingWorkspace:
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)
    return ws


def _roster(members=None):
    members = members or _MEMBERS
    pairs = [(m["id"], m["metadata"]["coding_role"]) for m in members
             if m.get("enabled", True)]
    return pairs, members_by_coding_role(members)


def _choices(store: LedgerStore) -> list[str]:
    return [str(d.get("choice") or "") for d in store.list_decisions()]


# --------------------------------------------------------------------------- #
# S1 — the closure verdict. Three outcomes, one computation.
# --------------------------------------------------------------------------- #

def test_default_council_on_an_empty_master_is_the_test_that_would_have_caught_it(
        tmp_path: Path) -> None:
    """THE default-council truth. A stock ``CodingAutonomyPolicy()`` + an empty
    master + the four-role roster the product ships: TESTER is ``deferred`` and
    REVIEWER is ``unclosable``, on the LIVE defaults. A future flip of
    ``reviewer_repo_read`` shows up here first."""
    s = _store("cl-default", tmp_path)
    policy = CodingAutonomyPolicy()
    # The two shipped defaults this whole spec is written to survive.
    assert policy.reviewer_repo_read is False
    assert policy.capability_overrides == {}

    man = capability_manifest(s, policy)
    verdicts = {v.role: v for v in role_closure(
        man, seated_roles=(PM, DEV, REVIEWER, TESTER))}

    assert verdicts[PM].outcome == CAPABLE
    assert verdicts[DEV].outcome == CAPABLE
    assert verdicts[REVIEWER].outcome == UNCLOSABLE
    assert verdicts[TESTER].outcome == DEFERRED
    # The reason text is GL03's message, verbatim — one source, not a paraphrase.
    assert "REVIEWER" in verdicts[REVIEWER].reason and "SPEC-14" in verdicts[REVIEWER].reason
    assert "TESTER" in verdicts[TESTER].reason and "SPEC-12" in verdicts[TESTER].reason
    # Every non-capable verdict carries the ONE action that closes it.
    assert verdicts[REVIEWER].remedy and verdicts[TESTER].remedy
    assert verdicts[PM].remedy == "" and verdicts[DEV].remedy == ""


def test_granting_reviewer_repo_read_moves_it_to_capable_with_no_other_change(
        tmp_path: Path) -> None:
    s = _store("cl-grant", tmp_path)
    man = capability_manifest(s, CodingAutonomyPolicy(reviewer_repo_read=True))
    verdicts = {v.role: v for v in role_closure(
        man, seated_roles=(PM, DEV, REVIEWER, TESTER))}
    assert verdicts[REVIEWER].outcome == CAPABLE
    assert verdicts[TESTER].outcome == DEFERRED     # unchanged — different capability


def test_seated_roles_scope_the_closure(tmp_path: Path) -> None:
    """PM+DEV — the single-agent-plus-coordination baseline the entire RQ5
    principle defends. This spec must never make a PM+DEV run ask anything."""
    s = _store("cl-scope", tmp_path)
    man = capability_manifest(s, CodingAutonomyPolicy())
    verdicts = role_closure(man, seated_roles=(PM, DEV))
    assert [v.outcome for v in verdicts] == [CAPABLE, CAPABLE]
    assert all(v.remedy == "" for v in verdicts)


def test_an_override_changes_the_consequence_never_the_finding(
        tmp_path: Path) -> None:
    s = _store("cl-ovr", tmp_path)
    man = capability_manifest(s, CodingAutonomyPolicy())
    plain = {v.role: v for v in role_closure(man, seated_roles=(TESTER,))}
    over = {v.role: v for v in role_closure(
        man, seated_roles=(TESTER,), overrides=frozenset({TESTER}))}
    # Same verdict, same reason, same remedy — only `overridden`/`seatable` differ.
    assert over[TESTER].outcome == plain[TESTER].outcome == DEFERRED
    assert over[TESTER].reason == plain[TESTER].reason
    assert over[TESTER].overridden is True and plain[TESTER].overridden is False
    assert over[TESTER].seatable is True and plain[TESTER].seatable is False


@pytest.mark.parametrize("raw, expected", [
    ({}, set()),                                    # the disable value
    ({"tester": True}, {"tester"}),                 # the persisted shape
    ({"TESTER": 1}, {"tester"}),                    # case/truthiness tolerant
    ({"tester": False}, set()),                     # explicit off is off
    ({"tester": {"execution": True}}, {"tester"}),  # per-capability shape
    ({"tester": {"execution": False}}, set()),
    (["tester", "reviewer"], {"tester", "reviewer"}),  # the spec's JSON example
    ("tester", set()),                              # malformed -> today's behaviour
    (None, set()),
])
def test_capability_override_roles_normalises_every_shipped_shape(raw, expected):
    class _P:
        capability_overrides = raw
    assert capability_override_roles(_P()) == expected


def test_capability_overrides_survive_the_policy_round_trip() -> None:
    p = CodingAutonomyPolicy(capability_overrides={"tester": True})
    assert policy_from_dict(policy_to_dict(p)).capability_overrides == {"tester": True}


# --------------------------------------------------------------------------- #
# S1 — the false-resolve lock, at the manifest level. THE most important pair.
# --------------------------------------------------------------------------- #

def test_the_tester_predicate_is_unit_commands_not_the_engine_gate(
        tmp_path: Path) -> None:
    """The bug that reshaped the spec. ``gate_available`` answers the ENGINE's
    question ("is there an acceptance gate that CAN produce evidence?"). The audit
    read it and reported the answer as the TESTER's. They are different facts, and
    every way the engine's answer can flip True leaves the tester undispatchable."""
    s = _store("cl-pred", tmp_path)

    # 1. Empty project: neither.
    assert _tester_dispatchable(s) is False
    assert capability_manifest(s, None)[TESTER].gate_available is False

    # 2. An ACCEPTANCE command — all `gate_bootstrap` can ever register — flips the
    #    engine's gate and leaves the tester undispatchable.
    s.set_test_commands({"acc": _ACCEPTANCE})
    man = capability_manifest(s, None)
    assert man[TESTER].gate_available is True
    assert man[TESTER].can_dispatch is False
    assert _tester_dispatchable(s) is False
    assert role_closure(man, seated_roles=(TESTER,))[0].outcome == DEFERRED

    # 3. A UNIT command — the only thing that actually arms a tester turn.
    s.set_test_commands({"acc": _ACCEPTANCE, "unit": _UNIT})
    man = capability_manifest(s, None)
    assert man[TESTER].can_dispatch is True
    assert role_closure(man, seated_roles=(TESTER,))[0].outcome == CAPABLE


def test_a_runtime_profile_does_not_falsely_resolve_the_tester(
        tmp_path: Path) -> None:
    """The runtime-profile lock — the exact conflation the audit shipped with. A
    ``managed_local`` profile registered by ``gate_bootstrap`` after the first
    ``index.html`` merge flips ``gate_available`` True. It is a LAUNCH PROBE FOR THE
    ENGINE, not a command for the tester, and it must leave the seat deferred."""
    s = _store("cl-rt", tmp_path)
    from errorta_council.coding.runtime import RuntimeProfile, RuntimeProfileStore
    RuntimeProfileStore.for_ledger(s).upsert_profile(RuntimeProfile(
        profile_id="default", project_id=s.project_id, kind="static",
        runtime_mode="managed_local",
        start=["python", "-m", "http.server", "{port}"]))
    man = capability_manifest(s, CodingAutonomyPolicy())
    assert man[TESTER].gate_available is True    # the engine really did gain a gate
    assert man[TESTER].can_dispatch is False     # ...and the tester really did not
    assert role_closure(man, seated_roles=(TESTER,))[0].outcome == DEFERRED
    # The PM's capability segment still (correctly) describes the gate as available:
    # two different facts, no longer conflated into one.
    assert "(configured)" in capabilities.pm_capability_segment(s, CodingAutonomyPolicy())


# --------------------------------------------------------------------------- #
# S2 — the consequence: unseat, never refuse.
# --------------------------------------------------------------------------- #

def test_apply_role_closure_unseats_and_filters_both_roster_structures(
        tmp_path: Path) -> None:
    """The seat-time consequence on the shipped defaults. BOTH roster structures are
    filtered — filtering one seats a ghost (a role the scheduler skips but a turn can
    still resolve a member for)."""
    s = _store("cl-seat", tmp_path)
    pairs, by_role = _roster()
    closure = _apply_role_closure(s, pairs, by_role, CodingAutonomyPolicy())

    assert pairs == [("m-pm", PM), ("m-dev", DEV), ("m-rev", REVIEWER)]
    assert set(by_role) == {PM, DEV, REVIEWER}
    assert closure.seated(TESTER) is False
    assert closure.seated(PM) is True and closure.seated(DEV) is True
    # The finding is recorded and paged for BOTH flagged roles, exactly as the
    # advisory always was — the reviewer's seat is not a reason to go quiet.
    assert _choices(s).count("topology_advisory") == 2
    open_titles = [x.title for x in attention.list_open(s.project_id, store=s)]
    assert sum(1 for t in open_titles if t.startswith("topology advisory:")) == 2
    # ...and the signal now carries a context a RESOLUTION can key on (the old
    # `{"advisory": msg}` could not — a title prefix is not a key).
    ctxs = {sig.context.get("role"): sig.context
            for sig in attention.list_open(s.project_id, store=s)
            if sig.source == "topology_audit"}
    assert ctxs[TESTER]["capability"] == "execution"
    assert ctxs[TESTER]["outcome"] == DEFERRED
    assert ctxs[REVIEWER]["outcome"] == UNCLOSABLE
    # Published for the operator surfaces / SPEC-24's snapshot row.
    published = s.get_run_state().get("role_closure") or {}
    assert published["seated"] == [DEV, PM, REVIEWER]
    assert published["unseated"] == [TESTER]
    assert len(published["verdicts"]) == 4


def test_a_role_whose_unseat_would_wedge_the_run_is_seated_under_protest(
        tmp_path: Path) -> None:
    """SPEC-26 Item 2's premise — *"unseating is free and already correct"* — is
    TRUE for the TESTER (once S4 couples the spawn and the merge gate) and FALSE for
    the REVIEWER: ``_set_mergeable_if_ready`` requires ``reviewer_approved`` and is
    the ONLY writer of ``mergeable``, so a reviewer-less council leaves every PR at
    ``open`` and ends ``completion_blocked`` with an empty master.

    So the ungrounded REVIEWER keeps its seat and gets a LOUDER finding, not a
    quieter one. Making it genuinely unseatable needs a reviewer-less merge path —
    a product trust-boundary decision, not a side effect of a capability audit."""
    import errorta_council.coding.runner as r

    s = _store("cl-protest", tmp_path)
    pairs, by_role = _roster()
    closure = _apply_role_closure(s, pairs, by_role, CodingAutonomyPolicy())

    assert set(r._UNSEAT_BREAKS_THE_PIPELINE) == {DEV, REVIEWER}
    assert closure.verdicts[REVIEWER].outcome == UNCLOSABLE   # verdict UNCHANGED
    assert closure.seated(REVIEWER) is True                   # ...consequence is not
    unclosed = [d for d in s.list_decisions()
                if d["choice"] == "role_capability_unclosed"]
    assert [d["title"] for d in unclosed] == ["role capability unclosed: reviewer"]
    assert "wedge" in unclosed[0]["rationale"]
    assert "SPEC-14" in unclosed[0]["rationale"]              # names the remedy
    # And the lock that keeps the reason honest: the merge gate really does demand
    # reviewer approval unconditionally, and really is the only writer of
    # `mergeable`. If either stops being true, unseating the reviewer becomes safe
    # and this carve-out should go.
    import inspect
    src = inspect.getsource(r._apply_merge_gate)
    assert 'p.get("reviewer_approved") is True and tests_ok' in src
    whole = inspect.getsource(r)
    assert whole.count('store.update_pr(pr_id, status="mergeable")') == 1


def test_a_pm_dev_council_is_never_asked_anything(tmp_path: Path) -> None:
    s = _store("cl-pmdev", tmp_path)
    pairs, by_role = _roster(_MEMBERS[:2])
    closure = _apply_role_closure(s, pairs, by_role, CodingAutonomyPolicy())
    assert pairs == [("m-pm", PM), ("m-dev", DEV)]
    assert closure.unseated == {}
    assert s.list_decisions() == []
    assert attention.list_open(s.project_id, store=s) == []


def test_a_fully_capable_council_seats_all_four(tmp_path: Path) -> None:
    s = _store("cl-full", tmp_path)
    s.set_test_commands({"unit": _UNIT})
    pairs, by_role = _roster()
    closure = _apply_role_closure(
        s, pairs, by_role, CodingAutonomyPolicy(reviewer_repo_read=True))
    assert len(pairs) == 4 and closure.unseated == {}
    assert s.list_decisions() == []


def test_an_override_seats_an_uncapable_tester_and_says_why(tmp_path: Path) -> None:
    """The operator who WANTS an idle TESTER on the board. The cost is honest and
    bounded — a roster slot, not model calls — and the recorded decision is what
    answers 'why did my tester never do anything?' with something other than
    silence."""
    s = _store("cl-seatanyway", tmp_path)
    pairs, by_role = _roster()
    policy = CodingAutonomyPolicy(capability_overrides={"tester": True})
    closure = _apply_role_closure(s, pairs, by_role, policy)

    assert ("m-test", TESTER) in pairs          # seated
    assert TESTER in by_role
    assert closure.seated(TESTER) is True
    assert closure.verdicts[TESTER].outcome == DEFERRED     # verdict UNCHANGED
    assert "role_capability_seated" in _choices(s)
    assert "topology_advisory" in _choices(s)               # finding still recorded


def test_evaluation_failure_fails_open_on_the_roster_and_says_so(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A check whose whole purpose is to refuse things must not let a ledger hiccup
    empty a council. Fail OPEN — but never silently."""
    import errorta_council.coding.runner as r

    s = _store("cl-open", tmp_path)

    def _boom(*_a, **_k):
        raise RuntimeError("ledger hiccup")

    monkeypatch.setattr(r._capabilities, "capability_manifest", _boom)
    pairs, by_role = _roster()
    closure = _apply_role_closure(s, pairs, by_role, CodingAutonomyPolicy())

    assert len(pairs) == 4 and set(by_role) == {PM, DEV, REVIEWER, TESTER}
    assert closure.unseated == {} and closure.indeterminate is True
    assert "role_capability_indeterminate" in _choices(s)
    assert (s.get_run_state().get("role_closure") or {})["indeterminate"] is True


def test_dev_is_never_unseated_even_when_unclosable(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one refusal-shaped case, stated so the boundary is a decision rather
    than an omission. Unreachable today (``_ROLE_TOOLS[DEV]`` is a non-empty
    constant); a regression tripwire, not a feature. Unseating the producer would
    leave a council that cannot produce work at all."""
    import errorta_council.coding.turn_controller as tc

    monkeypatch.setattr(tc, "_ROLE_TOOLS", {**tc._ROLE_TOOLS, DEV: ()})
    s = _store("cl-dev", tmp_path)
    pairs, by_role = _roster(_MEMBERS[:2])
    closure = _apply_role_closure(s, pairs, by_role, CodingAutonomyPolicy())

    assert closure.verdicts[DEV].outcome == UNCLOSABLE
    assert ("m-dev", DEV) in pairs and DEV in by_role      # seated regardless
    assert "role_capability_unclosed" in _choices(s)


# --------------------------------------------------------------------------- #
# S3 — re-evaluation, re-seating, and the resolution that has never happened.
# --------------------------------------------------------------------------- #

def test_acceptance_registration_does_not_resolve_but_unit_registration_does(
        tmp_path: Path) -> None:
    """THE false-resolve lock. Same fixture, two registrations, opposite outcomes —
    that pair IS the spec. A false resolve is strictly worse than never resolving,
    because it launders an unclosed gap into a green check."""
    s = _store("cl-resolve", tmp_path)
    pairs, by_role = _roster()
    closure = _apply_role_closure(s, pairs, by_role, CodingAutonomyPolicy())
    assert closure.seated(TESTER) is False

    # (a) an ACCEPTANCE command — the only kind `gate_bootstrap` can produce.
    s.set_test_commands({"acc": _ACCEPTANCE})
    _reevaluate_role_closure(closure)
    assert closure.seated(TESTER) is False
    assert "role_capability_closed" not in _choices(s)
    assert any(sig.source == "topology_audit" and sig.context.get("role") == TESTER
               for sig in attention.list_open(s.project_id, store=s))

    # (b) a UNIT command — the capability the TESTER's duty actually needs.
    s.set_test_commands({"acc": _ACCEPTANCE, "unit": _UNIT})
    _reevaluate_role_closure(closure)

    assert closure.seated(TESTER) is True
    assert ("m-test", TESTER) in pairs and TESTER in by_role
    assert _choices(s).count("role_capability_closed") == 1
    closed = next(d for d in s.list_decisions()
                  if d["choice"] == "role_capability_closed")
    assert "unit" in closed["rationale"]           # names WHAT closed it
    # The advisory finally goes to `dismissed` — the half that has never happened.
    assert not any(sig.source == "topology_audit" and sig.context.get("role") == TESTER
                   for sig in attention.list_open(s.project_id, store=s))
    # The REVIEWER's advisory is untouched: a different capability, still unclosed.
    assert any(sig.context.get("role") == REVIEWER
               for sig in attention.list_open(s.project_id, store=s))
    # The ORIGINAL advisory decision is left verbatim — the ledger is append-only and
    # the pair (advisory at iteration 0, closed at iteration N) IS the trace.
    assert _choices(s).count("topology_advisory") == 2


def test_a_run_where_the_capability_never_arrives_leaves_the_signal_open(
        tmp_path: Path) -> None:
    """Roadmap criterion #4 satisfied in the NEGATIVE: the advisory stays open, and
    that is now TRUE rather than merely unresolved — the role is not seated."""
    s = _store("cl-never", tmp_path)
    pairs, by_role = _roster()
    closure = _apply_role_closure(s, pairs, by_role, CodingAutonomyPolicy())
    for _ in range(3):
        _reevaluate_role_closure(closure)
    assert closure.seated(TESTER) is False
    assert "role_capability_closed" not in _choices(s)
    assert any(sig.context.get("role") == TESTER
               for sig in attention.list_open(s.project_id, store=s))


def test_arm_gate_after_merge_is_the_reevaluation_hook(tmp_path: Path) -> None:
    """Item 3's hook: the merge point is the one quiescent moment the runner already
    re-derives gate state at, so re-seating rides the path that already exists."""
    s = _store("cl-merge", tmp_path)
    ws = _ws("cl-merge", s)
    pairs, by_role = _roster()
    closure = _apply_role_closure(s, pairs, by_role, CodingAutonomyPolicy())
    assert closure.seated(TESTER) is False

    s.set_test_commands({"unit": _UNIT})
    _arm_gate_after_merge(s, ws, changed=["src/app.js"], head="h1", closure=closure)
    assert closure.seated(TESTER) is True
    # ...and it is harmless without a closure (every non-production caller).
    _arm_gate_after_merge(s, ws, changed=["src/app.js"], head="h2")


def test_a_reseated_role_can_be_assigned_on_the_next_iteration(
        tmp_path: Path) -> None:
    """Re-seating is an append to the list object both loops re-read every
    iteration — no ``RunTurn`` signature change, no new machinery."""
    s = _store("cl-reseat", tmp_path)
    pairs, by_role = _roster()
    closure = _apply_role_closure(s, pairs, by_role, CodingAutonomyPolicy())

    dev_task = s.add_task(title="build", role=DEV)
    pr = s.record_pr(task_id=dev_task.task_id, branch="task-x", head="h",
                     dev_member="m-dev")
    s.update_pr(pr["pr_id"], reviewer_approved=True, reviewed_head="h")
    s.update_task(dev_task.task_id, state="done")
    test_task = s.add_task(title="test PR: task-x", role=TESTER, pr_id=pr["pr_id"])

    # Unseated: the scheduler cannot see the tester's work at all.
    assert not any(isinstance(a := decide_next(s, pairs, None), Assign)
                   and a.role == TESTER for _ in [0])

    s.set_test_commands({"unit": _UNIT})
    _reevaluate_role_closure(closure)
    action = decide_next(s, pairs, None)
    assert isinstance(action, Assign)
    assert action.role == TESTER and action.task_id == test_task.task_id


def test_the_concurrent_pool_is_sized_for_a_role_that_can_be_reseated(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one hazard in Item 3, and its one-line fix. ``_run_concurrent_loop`` sizes
    its pool ONCE, before the loop; a role appended afterwards would push
    ``runtime_cap`` above the pool and silently serialize dispatch (a too-small pool
    degrades quietly rather than raising, which is why this is spied, not inferred).
    The pool is an upper bound only, so widening it is strictly safe."""
    import concurrent.futures as cf

    s = _store("cl-pool", tmp_path)
    seen: dict[str, int | None] = {}
    real = cf.ThreadPoolExecutor

    class _Spy(real):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            seen["max_workers"] = kw.get("max_workers")
            super().__init__(*a, **kw)

    monkeypatch.setattr(cf, "ThreadPoolExecutor", _Spy)
    full, _ = _roster()                                   # 3 non-PM workers
    seated = [p for p in full if p[1] != TESTER]          # 2 after closure

    assert effective_parallelism(CodingAutonomyPolicy(), seated) == 2
    assert effective_parallelism(CodingAutonomyPolicy(), full) == 3

    run_coding_loop(s, seated, CodingAutonomyPolicy(),
                    run_turn=lambda _a, _l: None, should_cancel=lambda: True,
                    pool_members=full)
    assert seen["max_workers"] == 3 + 2                   # full roster + headroom

    # Without `pool_members` nothing changes for a run with no deferred roles.
    seen.clear()
    run_coding_loop(s, seated, CodingAutonomyPolicy(),
                    run_turn=lambda _a, _l: None, should_cancel=lambda: True)
    assert seen["max_workers"] == 2 + 2


def test_a_closure_resolution_also_dismisses_the_gl03_confabulation_alert(
        tmp_path: Path) -> None:
    """GL03's confabulation alarm is the RUNTIME symptom of the same disease — an
    unclosed closure verdict observed one turn later instead of at seat time. Three
    producers, one alert: the resolution clears both for the same
    ``(role, capability)`` pair."""
    s = _store("cl-dedupe", tmp_path)
    pairs, by_role = _roster()
    closure = _apply_role_closure(s, pairs, by_role, CodingAutonomyPolicy())
    attention.raise_capability_gap_alert(
        s.project_id, role=TESTER, capability="execution",
        tool_name="run_tests", summary="", store=s)
    pair_open = [sig for sig in attention.list_open(s.project_id, store=s)
                 if sig.context.get("role") == TESTER
                 and sig.context.get("capability") == "execution"]
    assert len(pair_open) == 2                      # closure + confabulation

    s.set_test_commands({"unit": _UNIT})
    _reevaluate_role_closure(closure)
    assert not [sig for sig in attention.list_open(s.project_id, store=s)
                if sig.context.get("role") == TESTER]


# --------------------------------------------------------------------------- #
# S4 — the coupling. Unseating must NEVER wedge a run.
# --------------------------------------------------------------------------- #

def _approving_caller(review_task_id: str, head: str):
    def caller(_member, _prompt):
        return json.dumps({
            "schema_version": "coding_turn.v1", "role": "reviewer",
            "task_id": review_task_id,
            "intent": {"kind": "review_verdict", "reviewed_head": head,
                       "approved": True, "findings": []}})
    return caller


def _approve_pr(store, ws, *, closure):
    dev_task = store.add_task(title="build post card", role=DEV)
    ws.start_task_branch(dev_task.task_id)
    ws.write_file("app.js", "export const x = 1\n", task_id=dev_task.task_id)
    pr = store.record_pr(task_id=dev_task.task_id,
                         branch=ws.task_branch(dev_task.task_id),
                         head=ws.head(), dev_member="m-dev")
    review_task = store.add_task(title=f"review PR: {pr['branch']}", role=REVIEWER,
                                 pr_id=pr["pr_id"])
    rt = build_run_turn(store, ws, members_by_coding_role(_MEMBERS),
                        _approving_caller(review_task.task_id, pr["head"]),
                        guardrail_enabled=True, role_closure_state=closure)
    rt(Assign(member_id="m-rev", task_id=review_task.task_id, role=REVIEWER), store)
    return store.get_pr(pr["pr_id"])


def test_a_role_absent_from_the_roster_is_not_seated(tmp_path: Path) -> None:
    """Closure verdicts only cover supplied roles; absence must not become a seat."""
    s = _store("cl-absent", tmp_path)
    ws = _ws("cl-absent", s)
    s.set_test_commands({"unit": _UNIT})
    members = [m for m in _MEMBERS if m["metadata"]["coding_role"] != TESTER]
    pairs, by_role = _roster(members)
    closure = _apply_role_closure(
        s, pairs, by_role, CodingAutonomyPolicy(reviewer_repo_read=True))

    assert closure.seated(TESTER) is False
    assert closure.seated(PM) is True
    pr = _approve_pr(s, ws, closure=closure)
    assert pr["status"] == "mergeable"
    assert not [t for t in s.list_tasks() if t.title.startswith("test PR:")]


def test_an_unseated_tester_never_wedges_an_approved_pr(tmp_path: Path) -> None:
    """THE wedge regression, asserted rather than described. Without the coupling
    this test hangs the PR: the spawn creates a ``test PR:`` task nobody can take
    and ``_set_mergeable_if_ready`` holds the PR until ``tests_passed is True``.
    Trading an unread advisory for a wedge is not a fix."""
    s = _store("cl-wedge", tmp_path)
    ws = _ws("cl-wedge", s)
    s.set_test_commands({"unit": _UNIT})            # a real unit command exists
    pairs, by_role = _roster()
    # Force the TESTER off the board even though the project HAS a unit command —
    # the exact configuration in which unseating used to be unsafe.
    closure = _apply_role_closure(s, pairs, by_role, CodingAutonomyPolicy())
    closure.unseat(closure.verdicts[TESTER] if TESTER in closure.verdicts
                   else role_closure(capability_manifest(s, None),
                                     seated_roles=(TESTER,))[0])
    assert closure.seated(TESTER) is False

    pr = _approve_pr(s, ws, closure=closure)

    assert pr["status"] == "mergeable"              # review approval alone governs
    assert not [t for t in s.list_tasks() if t.title.startswith("test PR:")]


def test_stale_base_revalidation_spawns_no_phantom_task_for_an_unseated_tester(
        tmp_path: Path) -> None:
    """The SECOND wedge, and the subtler one. ``_revalidate_stale_prs`` demotes every
    other mergeable PR after a merge and parks it behind a ``re-test PR:`` task. With
    the TESTER unseated that task can never be dispatched — and a non-terminal task
    blocks the completion claim (``pending_completion_work``), so an otherwise
    finished run would end ``completion_blocked`` instead of ``definition_of_done``.

    Today's not-applicable tester turn produces exactly the state asserted here
    (mergeable against the newly integrated head); SPEC-26 reaches it without a turn
    nobody can take."""
    import errorta_council.coding.runner as r

    s = _store("cl-stale", tmp_path)
    ws = _ws("cl-stale", s)
    pairs, by_role = _roster()
    closure = _apply_role_closure(s, pairs, by_role, CodingAutonomyPolicy())
    assert closure.seated(TESTER) is False

    # Two PRs on independent files; one merges, the other is revalidated.
    a = s.add_task(title="a", role=DEV)
    ws.start_task_branch(a.task_id)
    ws.write_file("a.js", "export const a = 1\n", task_id=a.task_id)
    pr_a = s.record_pr(task_id=a.task_id, branch=ws.task_branch(a.task_id),
                       head=ws.head(), dev_member="m-dev")
    b = s.add_task(title="b", role=DEV)
    ws.start_task_branch(b.task_id)
    ws.write_file("b.js", "export const b = 1\n", task_id=b.task_id)
    pr_b = s.record_pr(task_id=b.task_id, branch=ws.task_branch(b.task_id),
                       head=ws.head(), dev_member="m-dev")
    s.update_pr(pr_b["pr_id"], reviewer_approved=True, reviewed_head=pr_b["head"],
                tests_passed=True, status="mergeable")
    assert ws.merge_pr(pr_a["branch"]).get("merged")

    r._revalidate_stale_prs(s, ws, just_merged_pr_id=pr_a["pr_id"])

    assert not [t for t in s.list_tasks() if t.title.startswith("re-test PR:")]
    assert s.get_pr(pr_b["pr_id"])["status"] == "mergeable"
    assert any(d["choice"] == "stale_base_revalidation" for d in s.list_decisions())


def test_a_seated_tester_still_gates_the_merge_exactly_as_before(
        tmp_path: Path) -> None:
    """The other half of the lock: the strict reviewer-AND-tests gate is untouched
    for a council that actually has a tester. SPEC-26 relaxes nothing on its own."""
    s = _store("cl-nowedge", tmp_path)
    ws = _ws("cl-nowedge", s)
    s.set_test_commands({"unit": _UNIT})
    pairs, by_role = _roster()
    closure = _apply_role_closure(
        s, pairs, by_role, CodingAutonomyPolicy(reviewer_repo_read=True))
    assert closure.seated(TESTER) is True           # a unit command exists -> capable

    pr = _approve_pr(s, ws, closure=closure)

    assert pr["status"] != "mergeable"              # held for the tester verdict
    assert [t for t in s.list_tasks() if t.title.startswith("test PR:")]


def test_no_closure_state_reproduces_todays_behaviour_exactly(
        tmp_path: Path) -> None:
    """Every direct ``build_run_turn`` caller passes no closure. That must mean
    'nothing was unseated', not 'nothing is seated'."""
    s = _store("cl-legacy", tmp_path)
    ws = _ws("cl-legacy", s)
    s.set_test_commands({"unit": _UNIT})
    pr = _approve_pr(s, ws, closure=None)
    assert pr["status"] != "mergeable"
    assert [t for t in s.list_tasks() if t.title.startswith("test PR:")]


# --------------------------------------------------------------------------- #
# Item 5 — the invariant, and the lock that keeps it true.
# --------------------------------------------------------------------------- #

def test_every_manifest_role_has_a_duty_to_capability_entry(tmp_path: Path) -> None:
    """TOTAL COVERAGE. A fifth role added to ``capability_manifest`` without a
    capability story fails HERE, and this is the only place in the tree that would
    catch it."""
    s = _store("cl-lock1", tmp_path)
    produced = set(capability_manifest(s, CodingAutonomyPolicy()))
    missing = produced - set(CLOSURE_TABLE)
    assert not missing, (
        f"roles produced by capability_manifest with no CLOSURE_TABLE entry: "
        f"{sorted(missing)} — every role needs a duty→capability story (SPEC-26 "
        f"Item 5). Add it to capabilities.CLOSURE_TABLE with an outcome and a remedy.")
    # ...and the CLI's own role list cannot drift past it either.
    from errorta_cli.teamdraft import CODING_ROLES
    assert set(CODING_ROLES) <= set(CLOSURE_TABLE)


def test_the_three_outcomes_are_stable(tmp_path: Path) -> None:
    """The truth table, in the style ``test_gl05_parallelism`` uses for the advisory
    this replaces. Each row is a capability flag combination and its outcome."""
    s = _store("cl-lock2", tmp_path)
    man = capability_manifest(s, CodingAutonomyPolicy())

    def _with(role, **kw):
        return {**man, role: capabilities.RoleCapability(
            **{**man[role].to_dict(), "tools": tuple(man[role].tools), **kw})}

    def _outcome(manifest, role):
        return role_closure(manifest, seated_roles=(role,))[0].outcome

    assert _outcome(man, PM) == CAPABLE                                   # always
    assert _outcome(man, DEV) == CAPABLE                                  # has tools
    assert _outcome(_with(DEV, tools=()), DEV) == UNCLOSABLE
    assert _outcome(man, REVIEWER) == UNCLOSABLE                          # read off
    assert _outcome(_with(REVIEWER, repo_read=True), REVIEWER) == CAPABLE
    assert _outcome(man, TESTER) == DEFERRED                              # no command
    assert _outcome(_with(TESTER, can_dispatch=True), TESTER) == CAPABLE
    # `gate_available` alone NEVER moves the tester — the whole point.
    assert _outcome(_with(TESTER, gate_available=True), TESTER) == DEFERRED


def test_closure_at_run_start_holds_over_every_seated_subset(
        tmp_path: Path) -> None:
    """THE INVARIANT, property-style over all 2^4 seated subsets × the capability
    combinations that matter: after ``_apply_role_closure`` every role still in
    ``member_pairs`` is ``capable``, or named in ``capability_overrides``, or a role
    whose unseat would wedge the run (recorded loudly and seated under protest
    instead — DEV and REVIEWER, each justified against the code in
    ``_UNSEAT_BREAKS_THE_PIPELINE``). There is no fourth state."""
    import itertools

    import errorta_council.coding.runner as r

    roles = (PM, DEV, REVIEWER, TESTER)
    n = 0
    for size in range(len(roles) + 1):
        for subset in itertools.combinations(roles, size):
            for reviewer_read, unit_cmd, overridden in itertools.product(
                    (False, True), repeat=3):
                n += 1
                s = _store(f"cl-inv{n}", tmp_path)
                if unit_cmd:
                    s.set_test_commands({"unit": _UNIT})
                policy = CodingAutonomyPolicy(
                    reviewer_repo_read=reviewer_read,
                    capability_overrides=({r: True for r in roles}
                                          if overridden else {}))
                members = [m for m in _MEMBERS
                           if m["metadata"]["coding_role"] in subset]
                pairs, by_role = _roster(members)
                closure = _apply_role_closure(s, pairs, by_role, policy)
                seated = {role for _mid, role in pairs}
                assert seated == set(by_role)        # the two structures agree
                overrides = capability_override_roles(policy)
                for role in seated:
                    verdict = closure.verdicts.get(role)
                    assert verdict is not None
                    assert (verdict.outcome == CAPABLE
                            or role in overrides
                            or role in r._UNSEAT_BREAKS_THE_PIPELINE), (
                        f"seated {role} with outcome {verdict.outcome} and no "
                        f"override — the invariant has a fourth state")
                    # ...and a role seated under protest always leaves a trace.
                    if (verdict.outcome != CAPABLE and role not in overrides):
                        assert "role_capability_unclosed" in _choices(s)
