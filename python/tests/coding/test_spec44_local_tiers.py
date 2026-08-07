"""SPEC-44 — local tier derivation, a bounded visible downgrade, a discriminated rung.

**The trap this file exists to avoid.** "A single-model deployment" is naturally
written as a member with ``model_mode="single"`` — and such a member returns at
``model_assignment.py:89`` *before the selector is ever consulted*, so a test written
that way passes vacuously and asserts nothing about this spec. Every case below uses
``model_mode="multi"`` with a pool of exactly one route, which is the input shape that
actually reaches the hard exclusion at ``model_selector.py:71``.

The defect being closed is not hypothetical: today ``tier_for_route`` returns ``mid``
for every ``local.*`` route before any name inspection, ``select`` *hard-excludes*
anything below the requested rank, and ``build_run_turn``'s ``Assign`` handler turns
the resulting sentinel into ``hard_blocker=True``. A ``strong`` task on an all-local
pool is unservable, permanently, with no rung that can recover it.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from errorta_council.coding.autonomy import (
    CodingAutonomyPolicy,
    LoopCounters,
    TurnOutcome,
    _handle_unproductive,
    policy_from_dict,
    policy_to_dict,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.model_assignment import (
    make_assignment,
    next_escalation_assignment,
    resolve_task_assignment,
)
from errorta_council.coding.model_availability import RouteAvailability
from errorta_council.coding.model_catalog import load_catalog
from errorta_council.coding.model_tier import tier_for_route, tier_rank
from errorta_council.coding.runner import build_run_turn, members_by_coding_role
from errorta_council.coding.topology import DEV, PM, Assign

# `ollama list` on the senditai reference box (RX 9060 XT 16 GB).
SEVEN_B = "local.qwen2.5-coder:7b"
NINE_B = "local.qwen3.5:9b"
FOURTEEN_B = "local.qwen2.5-coder:14b"
GEMMA_27B = "local.gemma3:27b"
MISTRAL = "local.mistral-small3.1:latest"
EMBED = "local.nomic-embed-text:latest"
REAL_POOL = [SEVEN_B, NINE_B, FOURTEEN_B, GEMMA_27B, MISTRAL, EMBED]

TIERS_ENV = "ERRORTA_LOCAL_SIZE_TIERS"


@pytest.fixture(autouse=True)
def _no_operator_overrides(monkeypatch):
    """The developer's real `~/.errorta/council/model-catalog-overrides.json` must
    not decide these assertions."""
    monkeypatch.setattr(
        "errorta_council.coding.model_catalog.load_overrides",
        lambda path=None: {},
    )


@pytest.fixture
def tiers_on(monkeypatch):
    monkeypatch.setenv(TIERS_ENV, "1")


@pytest.fixture
def tiers_off(monkeypatch):
    monkeypatch.delenv(TIERS_ENV, raising=False)


def _all_reachable(monkeypatch, unreachable: tuple[str, ...] = ()) -> None:
    monkeypatch.setattr(
        "errorta_council.coding.model_availability.resolve_route_availability",
        lambda routes: {
            route: RouteAvailability(
                route, route.split(".", 1)[0], route not in unreachable,
                "down" if route in unreachable else "",
            )
            for route in routes
        },
    )
    monkeypatch.setattr(
        "errorta_council.coding.performance_corpus.digest", lambda *a, **k: {})


def _task(**kw) -> SimpleNamespace:
    base = dict(
        task_id="t1", task_type="implementation", difficulty_tier="mid",
        preferred_route_id="", assignment_rationale="", model_assignment=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _multi_member(pool: list[str]) -> dict:
    """A `model_mode="multi"` member. NOT "single" — see the module docstring."""
    return {"id": "m-dev", "model_mode": "multi", "model_pool": list(pool)}


# --------------------------------------------------------------------------- #
# Move 1 — the derivation
# --------------------------------------------------------------------------- #

_DOCUMENTED_TIERS = [
    # (route, tier with the knob ON) — the Move 1c table, verbatim.
    (SEVEN_B, "light"),      # 78.7% tests / 45.8% full-task: the weakest measured
    (NINE_B, "mid"),         # 92.3% / 83.3%: the recommended model
    (FOURTEEN_B, "mid"),     # 74.9% / 37.5%
    (GEMMA_27B, "mid"),      # ~17 GB: does not fit — and NEVER `strong`
    (MISTRAL, "mid"),        # "3.1" is a version, not a size -> no declared count
    (EMBED, "mid"),          # no declared count
]


@pytest.mark.parametrize("route,expected", _DOCUMENTED_TIERS)
def test_derived_tier_matches_the_documented_table(route, expected, tiers_on) -> None:
    assert tier_for_route(route) == expected


@pytest.mark.parametrize("route,_expected", _DOCUMENTED_TIERS)
def test_every_local_route_is_mid_with_the_knob_off(route, _expected, tiers_off) -> None:
    """The escape hatch: unset reproduces `model_tier.py:41-42` byte for byte."""
    assert tier_for_route(route) == "mid"


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "OFF", " False "])
def test_every_documented_disable_value_restores_mid(value, monkeypatch) -> None:
    monkeypatch.setenv(TIERS_ENV, value)
    assert tier_for_route(SEVEN_B) == "mid"


def test_no_local_route_is_ever_strong_from_a_parameter_count(tiers_on) -> None:
    """`_LARGE_MIN_B` is NOT a capability edge. On the 16 GB card the only models a
    `>=24B -> strong` rule would promote are the ones that do not fit, and because
    the escalate rung admits only strictly-stronger routes, a derived `strong` would
    make the ladder's designed target an OOM."""
    for route in REAL_POOL + ["local.llama3:70b", "local.foo:405b"]:
        assert tier_for_route(route) != "strong"


def test_gemma27b_reaches_strong_only_through_an_operator_override(
        tmp_path: Path, tiers_on, monkeypatch) -> None:
    monkeypatch.undo()  # restore the real `load_overrides` for this one case
    monkeypatch.setenv(TIERS_ENV, "1")
    assert load_catalog([GEMMA_27B], tmp_path / "absent.json")[
        GEMMA_27B].capability_tier == "mid"
    override = tmp_path / "model-catalog-overrides.json"
    override.write_text(json.dumps({GEMMA_27B: {"capability_tier": "strong"}}), "utf-8")
    assert load_catalog([GEMMA_27B], override)[GEMMA_27B].capability_tier == "strong"


@pytest.mark.parametrize("route", ["fake.small", "fake.qwen2.5-coder:7b", "fake.dev"])
def test_fake_routes_are_mid_with_the_knob_on_and_off(route, monkeypatch) -> None:
    """The test seam is never derived — the suite relies on fake routes tying."""
    monkeypatch.delenv(TIERS_ENV, raising=False)
    assert tier_for_route(route) == "mid"
    monkeypatch.setenv(TIERS_ENV, "1")
    assert tier_for_route(route) == "mid"


def test_mixed_local_pool_yields_more_than_one_rank(tiers_on) -> None:
    ranks = {tier_rank(tier_for_route(route)) for route in REAL_POOL}
    assert len(ranks) > 1


def test_exhausted_light_task_escalates_from_the_7b_to_the_9b(
        tiers_on, monkeypatch) -> None:
    """Issue #82's dead ladder, alive. With every local route tied at `mid` this
    rung could never fire; the derived tiers give it somewhere to go."""
    _all_reachable(monkeypatch)
    current = make_assignment(
        task_id="t1", member_id="m-dev", route_id=SEVEN_B,
        task_type="implementation", difficulty_tier="light", rationale="",
        source="selector",
    )
    task = SimpleNamespace(
        model_assignment=current.to_dict(),
        _extras={"model_pool_snapshot": [SEVEN_B, NINE_B]},
    )
    nxt, reason = next_escalation_assignment(task)
    assert reason == ""
    assert nxt is not None and nxt.route_id == NINE_B


# --------------------------------------------------------------------------- #
# Move 2 — the downgrade, on the one-route `multi` pool
# --------------------------------------------------------------------------- #

def test_single_local_model_still_assigns_under_derived_tiers(
        tiers_on, monkeypatch) -> None:
    """THE gate for this spec. One-route `multi` pool, default-`mid` task: today
    this returns `NoCapableModel` and the task hard-blocks forever."""
    _all_reachable(monkeypatch)
    assignment, reason = resolve_task_assignment(
        _task(), _multi_member([SEVEN_B]), difficulty_downgrade_limit=1,
    )
    assert assignment is not None, "no NoCapableModel may escape to the caller"
    assert reason == ""
    assert assignment.route_id == SEVEN_B
    assert assignment.difficulty_tier == "light"      # what it ACTUALLY ran at
    assert assignment.difficulty_downgraded_from == "mid"   # what was requested


def test_single_local_model_bricks_with_the_knob_disabled(
        tiers_on, monkeypatch) -> None:
    """The escape-hatch assertion for `difficulty_downgrade_limit`: 0 reproduces
    today's trace exactly, right through to the hard blocker."""
    _all_reachable(monkeypatch)
    assignment, reason = resolve_task_assignment(
        _task(), _multi_member([SEVEN_B]), difficulty_downgrade_limit=0,
    )
    assert (assignment, reason) == (None, "no_capable_model")


def test_single_local_model_unchanged_with_tiers_off(tiers_off, monkeypatch) -> None:
    """Both knobs off: the 7b is `mid`, is selected at `mid`, and nothing is
    recorded as downgraded."""
    _all_reachable(monkeypatch)
    assignment, reason = resolve_task_assignment(
        _task(), _multi_member([SEVEN_B]), difficulty_downgrade_limit=1,
    )
    assert assignment is not None and reason == ""
    assert assignment.difficulty_tier == "mid"
    assert assignment.difficulty_downgraded_from == ""


def test_strong_task_on_all_local_pool_downgrades_to_mid(
        tiers_off, monkeypatch) -> None:
    """§4.2 of the hardware doc, measured: "Strong tasks are unservable." Closed —
    and note the tiers knob is OFF, so this is the pre-existing defect, not one
    Move 1 introduced."""
    _all_reachable(monkeypatch)
    assignment, reason = resolve_task_assignment(
        _task(difficulty_tier="strong"), _multi_member(REAL_POOL),
        difficulty_downgrade_limit=1,
    )
    assert assignment is not None and reason == ""
    assert assignment.difficulty_tier == "mid"
    assert assignment.difficulty_downgraded_from == "strong"


def test_two_rank_drop_is_refused(tiers_on, monkeypatch) -> None:
    """`strong -> light` is a different claim about the run. With the default limit
    of 1 it is not reachable, even though a `light` route is sitting right there."""
    _all_reachable(monkeypatch)
    assignment, reason = resolve_task_assignment(
        _task(difficulty_tier="strong"), _multi_member([SEVEN_B]),
        difficulty_downgrade_limit=1,
    )
    assert (assignment, reason) == (None, "no_capable_model")


def test_two_rank_drop_is_reachable_only_at_the_explicit_limit(
        tiers_on, monkeypatch) -> None:
    _all_reachable(monkeypatch)
    assignment, _ = resolve_task_assignment(
        _task(difficulty_tier="strong"), _multi_member([SEVEN_B]),
        difficulty_downgrade_limit=2,
    )
    assert assignment is not None
    assert assignment.difficulty_tier == "light"
    assert assignment.difficulty_downgraded_from == "strong"


def test_unavailable_never_triggers_a_downgrade(tiers_on, monkeypatch) -> None:
    """A lower requested tier cannot make an unreachable route reachable.
    Downgrading here would fabricate a capability claim out of a network fault."""
    _all_reachable(monkeypatch, unreachable=(SEVEN_B,))
    assignment, reason = resolve_task_assignment(
        _task(), _multi_member([SEVEN_B]), difficulty_downgrade_limit=2,
    )
    assert (assignment, reason) == (None, "unavailable")


def test_empty_pool_never_triggers_a_downgrade(tiers_on, monkeypatch) -> None:
    _all_reachable(monkeypatch)
    assignment, reason = resolve_task_assignment(
        _task(), _multi_member([]), difficulty_downgrade_limit=2,
    )
    assert (assignment, reason) == (None, "empty_pool")


def test_next_escalation_assignment_never_downgrades(tiers_on, monkeypatch) -> None:
    """Escalation is supposed to be able to find nothing. The downgrade is an ENTRY
    condition for work, not a recovery rung — so the escalate path gets no fallback
    and no knob to give it one."""
    import inspect

    _all_reachable(monkeypatch)
    params = inspect.signature(next_escalation_assignment).parameters
    assert "difficulty_downgrade_limit" not in params
    current = make_assignment(
        task_id="t1", member_id="m-dev", route_id=NINE_B,
        task_type="implementation", difficulty_tier="mid", rationale="",
        source="selector",
    )
    task = SimpleNamespace(
        model_assignment=current.to_dict(),
        # The 7b is `light` with the knob on: a downgrade would happily take it.
        _extras={"model_pool_snapshot": [NINE_B, SEVEN_B]},
    )
    assert next_escalation_assignment(task) == (None, "no_capable_model")


def test_downgraded_assignment_is_reused_not_reminted(tiers_on, monkeypatch) -> None:
    """Constraint 5 at the unit level. `difficulty` is re-derived from the task every
    turn, so without the amended guard a downgraded assignment fails it FOREVER."""
    _all_reachable(monkeypatch)
    first, _ = resolve_task_assignment(
        _task(), _multi_member([SEVEN_B]), difficulty_downgrade_limit=1,
    )
    assert first is not None
    second, _ = resolve_task_assignment(
        _task(model_assignment=first.to_dict()), _multi_member([SEVEN_B]),
        difficulty_downgrade_limit=1,
    )
    assert second is not None
    assert second.assignment_id == first.assignment_id


def test_old_persisted_rows_load_without_the_new_field() -> None:
    from errorta_council.coding.model_assignment import ModelAssignment

    legacy = {
        "assignment_id": "ma-old", "task_id": "t", "member_id": "m",
        "route_id": SEVEN_B, "task_type": "implementation",
        "difficulty_tier": "mid", "rationale": "", "source": "selector",
        "assigned_at": "2026-01-01T00:00:00Z",
    }
    loaded = ModelAssignment.from_dict(legacy)
    assert loaded is not None and loaded.difficulty_downgraded_from == ""


# --------------------------------------------------------------------------- #
# Move 2 — through the runner's `Assign` handler (the hard-blocker seam)
# --------------------------------------------------------------------------- #

def _run_turn_for(store, pool, *, difficulty_downgrade_limit):
    members = [{
        "id": "m-dev", "enabled": True, "model_mode": "multi",
        "model_pool": list(pool), "metadata": {"coding_role": DEV},
    }]
    return build_run_turn(
        store, None, members_by_coding_role(members), lambda m, p: "{}",
        guardrail_enabled=True,
        difficulty_downgrade_limit=difficulty_downgrade_limit,
    )


def _store(tmp_path: Path, name: str) -> LedgerStore:
    store = LedgerStore(name, root=tmp_path)
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    return store


def _decisions(store, choice: str) -> list[dict]:
    return [d for d in store.list_decisions() if d.get("choice") == choice]


def test_assign_handler_serves_a_one_route_local_pool(
        tmp_path: Path, tiers_on, monkeypatch) -> None:
    """End to end at the seam that bricks today: `runner.py`'s `Assign` handler."""
    _all_reachable(monkeypatch)
    store = _store(tmp_path, "spec44-serve")
    task = store.add_task(title="implement", role=DEV, difficulty_tier="mid")
    run_turn = _run_turn_for(store, [SEVEN_B], difficulty_downgrade_limit=1)
    outcome = run_turn(Assign("m-dev", task.task_id, DEV), store)
    assert outcome.kind != "model_assignment_failed"
    assert not outcome.hard_blocker
    persisted = next(t for t in store.list_tasks() if t.task_id == task.task_id)
    assert persisted.model_assignment["route_id"] == SEVEN_B
    assert persisted.model_assignment["difficulty_tier"] == "light"
    # Constraint 1: visibility rides on the PERSISTED record, not on the
    # best-effort `record_decision` write.
    assert persisted.model_assignment["difficulty_downgraded_from"] == "mid"
    assert persisted._extras.get("model_difficulty_downgraded_from") == "mid"
    assert len(_decisions(store, "difficulty_downgraded")) == 1


def test_assign_handler_hard_blocks_with_the_knob_disabled(
        tmp_path: Path, tiers_on, monkeypatch) -> None:
    """The disable value reproduces today's trace exactly."""
    _all_reachable(monkeypatch)
    store = _store(tmp_path, "spec44-brick")
    task = store.add_task(title="implement", role=DEV, difficulty_tier="mid")
    run_turn = _run_turn_for(store, [SEVEN_B], difficulty_downgrade_limit=0)
    outcome = run_turn(Assign("m-dev", task.task_id, DEV), store)
    assert outcome.kind == "model_assignment_failed"
    assert outcome.hard_blocker is True
    assert outcome.reason == "no_capable_model"
    persisted = next(t for t in store.list_tasks() if t.task_id == task.task_id)
    assert persisted.state == "blocked"
    assert not _decisions(store, "difficulty_downgraded")


def test_assign_handler_writes_nothing_extra_with_tiers_off(
        tmp_path: Path, tiers_off, monkeypatch) -> None:
    _all_reachable(monkeypatch)
    store = _store(tmp_path, "spec44-off")
    task = store.add_task(title="implement", role=DEV, difficulty_tier="mid")
    run_turn = _run_turn_for(store, [SEVEN_B], difficulty_downgrade_limit=1)
    run_turn(Assign("m-dev", task.task_id, DEV), store)
    persisted = next(t for t in store.list_tasks() if t.task_id == task.task_id)
    assert persisted.model_assignment["difficulty_tier"] == "mid"
    assert persisted.model_assignment["difficulty_downgraded_from"] == ""
    assert not _decisions(store, "difficulty_downgraded")


def test_single_local_model_downgrade_is_recorded_once(
        tmp_path: Path, tiers_on, monkeypatch) -> None:
    """Constraint 5 at the seam that would churn: without the amended reuse guard
    this mints a fresh `assignment_id` and writes a duplicate decision EVERY turn."""
    _all_reachable(monkeypatch)
    store = _store(tmp_path, "spec44-once")
    task = store.add_task(title="implement", role=DEV, difficulty_tier="mid")
    run_turn = _run_turn_for(store, [SEVEN_B], difficulty_downgrade_limit=1)
    run_turn(Assign("m-dev", task.task_id, DEV), store)
    first = next(t for t in store.list_tasks() if t.task_id == task.task_id)
    first_id = first.model_assignment["assignment_id"]
    run_turn(Assign("m-dev", task.task_id, DEV), store)
    again = next(t for t in store.list_tasks() if t.task_id == task.task_id)
    assert again.model_assignment["assignment_id"] == first_id
    assert len(_decisions(store, "difficulty_downgraded")) == 1


# --------------------------------------------------------------------------- #
# Move 3 — an unavailable rung names which of five reasons
# --------------------------------------------------------------------------- #

def _escalation_task(route: str, snapshot: list[str] | None,
                     *, difficulty: str = "mid", assigned: bool = True):
    assignment = make_assignment(
        task_id="t1", member_id="m-dev", route_id=route,
        task_type="implementation", difficulty_tier=difficulty, rationale="",
        source="selector",
    ) if assigned else None
    return SimpleNamespace(
        model_assignment=assignment.to_dict() if assignment else None,
        _extras={"model_pool_snapshot": list(snapshot or [])},
    )


def test_rung_reason_no_current_assignment(monkeypatch) -> None:
    _all_reachable(monkeypatch)
    task = _escalation_task(SEVEN_B, [SEVEN_B], assigned=False)
    assert next_escalation_assignment(task) == (None, "no_current_assignment")


def test_rung_reason_empty_pool_snapshot(monkeypatch) -> None:
    _all_reachable(monkeypatch)
    assert next_escalation_assignment(
        _escalation_task(SEVEN_B, [])) == (None, "empty_pool_snapshot")


def test_escalation_rung_reason_on_single_model_pool(monkeypatch) -> None:
    """`all_routes_attempted`, NOT `no_capable_model`. With a one-route pool the
    candidate list is empty and `select` short-circuits at `model_selector.py:58-59`
    before the candidate loop ever runs — so SPEC-41's DoD clause naming
    `no_capable_model` for this shape was simply wrong."""
    _all_reachable(monkeypatch)
    assert next_escalation_assignment(
        _escalation_task(SEVEN_B, [SEVEN_B])) == (None, "all_routes_attempted")


def test_rung_reason_unavailable(monkeypatch) -> None:
    _all_reachable(monkeypatch, unreachable=(NINE_B,))
    assert next_escalation_assignment(
        _escalation_task(SEVEN_B, [SEVEN_B, NINE_B])) == (None, "unavailable")


def test_rung_reason_no_capable_model(monkeypatch) -> None:
    """The #82 case proper: a candidate remains, it is reachable, and it does not
    outrank the current route. Needs >=2 routes to reach at all."""
    _all_reachable(monkeypatch)
    task = _escalation_task("anthropic.sonnet", ["anthropic.sonnet", "openai.gpt-5"])
    assert next_escalation_assignment(task) == (None, "no_capable_model")


def test_five_rung_reasons_are_distinct() -> None:
    assert len({"no_current_assignment", "empty_pool_snapshot",
                "all_routes_attempted", "unavailable", "no_capable_model"}) == 5


def _unproductive(store, task, policy, counters, *, route: str):
    action = Assign(member_id="m-dev", task_id=task.task_id, role=DEV)
    outcome = TurnOutcome(
        kind="noop", unproductive=True, member_id="m-dev", member_role=DEV,
        member_route=route, reason="turn_non_json",
    )
    members = [("m-dev", DEV), ("pm", PM)]
    return _handle_unproductive(store, action, outcome, counters, policy, members)


def test_unavailable_rung_is_recorded_with_its_reason(
        tmp_path: Path, monkeypatch) -> None:
    _all_reachable(monkeypatch)
    store = _store(tmp_path, "spec44-rung")
    assignment = make_assignment(
        task_id="t", member_id="m-dev", route_id=SEVEN_B,
        task_type="implementation", difficulty_tier="mid", rationale="",
        source="selector",
    )
    task = store.add_task(title="implement", role=DEV, difficulty_tier="mid",
                          preferred_member_id="m-dev")
    store.update_task(task.task_id, model_assignment=assignment.to_dict(),
                      model_pool_snapshot=[SEVEN_B])
    policy = CodingAutonomyPolicy(worker_unproductive_limit=1,
                                  model_escalation_limit=2)
    _unproductive(store, task, policy, LoopCounters(), route=SEVEN_B)
    records = _decisions(store, "model_escalation_unavailable")
    assert len(records) == 1
    assert records[0]["reason"] == "all_routes_attempted"


def test_escalation_budget_exhausted_is_never_one_of_the_five(
        tmp_path: Path, monkeypatch) -> None:
    """A WALKED rung, not an absent one. Its accounting stays `c.model_escalations`."""
    _all_reachable(monkeypatch)
    store = _store(tmp_path, "spec44-budget")
    assignment = make_assignment(
        task_id="t", member_id="m-dev", route_id=SEVEN_B,
        task_type="implementation", difficulty_tier="mid", rationale="",
        source="selector",
    )
    task = store.add_task(title="implement", role=DEV, difficulty_tier="mid",
                          preferred_member_id="m-dev")
    # A pool with a genuinely stronger untried route: the ONLY thing stopping the
    # rung here is the budget, so the reason cannot be a hardware/pool reason.
    store.update_task(task.task_id, model_assignment=assignment.to_dict(),
                      model_pool_snapshot=[SEVEN_B, "anthropic.opus"])
    policy = CodingAutonomyPolicy(worker_unproductive_limit=1,
                                  model_escalation_limit=0)
    counters = LoopCounters()
    _unproductive(store, task, policy, counters, route=SEVEN_B)
    records = _decisions(store, "model_escalation_unavailable")
    assert len(records) == 1
    assert records[0]["reason"] == "escalation_budget_exhausted"
    assert records[0]["reason"] not in {
        "no_current_assignment", "empty_pool_snapshot", "all_routes_attempted",
        "unavailable", "no_capable_model"}
    assert counters.model_escalations == 0


def test_a_successful_rung_records_no_unavailable_decision(
        tmp_path: Path, monkeypatch) -> None:
    """Move 3 is purely additive: the record appears only on the None path."""
    _all_reachable(monkeypatch)
    store = _store(tmp_path, "spec44-rung-ok")
    assignment = make_assignment(
        task_id="t", member_id="m-dev", route_id="anthropic.haiku",
        task_type="implementation", difficulty_tier="light", rationale="",
        source="selector",
    )
    task = store.add_task(title="implement", role=DEV, difficulty_tier="light",
                          preferred_member_id="m-dev")
    store.update_task(task.task_id, model_assignment=assignment.to_dict(),
                      model_pool_snapshot=["anthropic.haiku", "anthropic.opus"])
    policy = CodingAutonomyPolicy(worker_unproductive_limit=1,
                                  model_escalation_limit=2)
    counters = LoopCounters()
    assert _unproductive(store, task, policy, counters,
                         route="anthropic.haiku") is None
    assert not _decisions(store, "model_escalation_unavailable")
    assert counters.model_escalations == 1


# --------------------------------------------------------------------------- #
# Move 5 — the policy knob is wired and documented
# --------------------------------------------------------------------------- #

def test_policy_knob_defaults_and_round_trips() -> None:
    assert CodingAutonomyPolicy().difficulty_downgrade_limit == 1
    assert policy_to_dict(CodingAutonomyPolicy())["difficulty_downgrade_limit"] == 1


@pytest.mark.parametrize("raw,expected", [
    (0, 0), (1, 1), (2, 2), (-5, 0), (9, 2),
])
def test_policy_knob_is_clamped(raw, expected) -> None:
    """`max(0, …)`-means-disabled, upper-clamped at 2 — there are only three tiers."""
    loaded = policy_from_dict({"difficulty_downgrade_limit": raw})
    assert loaded.difficulty_downgrade_limit == expected


def test_pm_reference_documents_the_knob() -> None:
    text = (Path(__file__).resolve().parents[3]
            / "docs" / "coding" / "PM_REFERENCE.md").read_text("utf-8")
    assert "difficulty_downgrade_limit" in text
