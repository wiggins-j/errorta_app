"""Spec 25 — expressibility and negotiation.

THE POINT OF THIS FILE. Four unsatisfiable-constraint bugs shipped and were fixed
one at a time; a fifth was found in the tree while the spec was being written.
Every one of them had the same shape: a state a run genuinely reaches, and no
turn the schema will accept for it. Point fixes cannot stop that class, because
the invariant it violates ("some turn is always legal") is not written down
anywhere — it is an emergent property of five independent validators in three
modules.

So it is written down here, as a test:

    For EVERY role in ``_INTENT_BY_ROLE``, a "nothing to do / blocked" turn is
    constructible and ``parse_coding_turn`` accepts it.

The table is driven off ``_INTENT_BY_ROLE`` itself, so adding a role without an
expressible blocked turn fails the build. The rest of the file locks the four
historical instances as regressions, checks that the corrective prompts teach a
shape the validator still accepts (one table, two consumers), and covers the
accounting split that stopped charging a shape rejection as idleness.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from errorta_council.coding import schemas as sc
from errorta_council.coding.autonomy import (
    CodingAutonomyPolicy,
    LoopCounters,
    TurnOutcome,
    _account_blocked_turn,
    _apply_outcome,
    policy_from_dict,
    policy_to_dict,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _corrective_turn_prompt,
    _humanize_parse_detail,
    _latest_context_response_text,
    _pm_turn_made_progress,
    build_run_turn,
    members_by_coding_role,
)
from errorta_council.coding.schemas import (
    BlockedIntent,
    CapabilityAsk,
    PMDecision,
    TurnErrorCode,
    TurnParseError,
    blocked_example,
    minimal_valid_example,
    parse_coding_turn,
)
from errorta_council.coding.topology import DEV, PM, REVIEWER, TESTER, Assign
from errorta_council.coding.turn_controller import tool_catalog_text

MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]

_ROLES = tuple(sc._INTENT_BY_ROLE)


def _envelope(role: str, intent: dict[str, Any], *, task_id: str | None) -> str:
    env: dict[str, Any] = {"schema_version": "coding_turn.v1", "role": role}
    if task_id is not None:
        env["task_id"] = task_id
    env["intent"] = intent
    return json.dumps(env)


def _task_id_for(role: str) -> str | None:
    # A PM plan turn is not bound to one task; every other role must answer for
    # exactly the assigned one.
    return None if role == PM else "t-1"


def _store(pid: str) -> LedgerStore:
    s = LedgerStore(pid)
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def _real_ws(pid: str, store: LedgerStore) -> Any:
    from errorta_council.coding.workspace import CodingWorkspace
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)
    return ws


# --------------------------------------------------------------------------- #
# 1. THE INVARIANT — every role can say "I am blocked".
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("role", _ROLES)
def test_every_role_can_express_a_blocked_turn(role: str) -> None:
    """Table-driven over ``_INTENT_BY_ROLE`` ITSELF. A new seat added there
    without an expressible blocked turn fails here — which is the whole
    difference between this and a fifth point fix."""
    out = parse_coding_turn(role, _task_id_for(role), _envelope(
        role, {"kind": "blocked", "reason": "missing_capability",
               "detail": "I have no way to run the tests this task requires."},
        task_id=_task_id_for(role)))
    assert not isinstance(out, TurnParseError), (role, out)
    assert isinstance(out.intent, BlockedIntent)
    assert out.intent.reason == "missing_capability"


@pytest.mark.parametrize("role", _ROLES)
def test_blocked_turn_requires_only_a_detail(role: str) -> None:
    """The single validator is ``detail`` non-empty — no cross-field rule that
    some other state could contradict. That is what makes the shape
    constructible from ANY state a model can be in."""
    out = parse_coding_turn(role, _task_id_for(role), _envelope(
        role, {"kind": "blocked", "detail": "stuck"},
        task_id=_task_id_for(role)))
    assert not isinstance(out, TurnParseError), (role, out)
    assert out.intent.reason == "other"      # defaulted, not required
    assert out.intent.needs is None          # optional, not required


@pytest.mark.parametrize("role", _ROLES)
def test_blocked_turn_survives_a_prose_reason_and_a_bare_needs(role: str) -> None:
    """A closed enum must not become a NEW way to make honesty inexpressible:
    prose in ``reason`` is kept (as detail, when detail is empty) and the label
    degrades to ``other``; a bare-string ``needs`` is accepted as a capability."""
    out = parse_coding_turn(role, _task_id_for(role), _envelope(
        role, {"kind": "blocked", "reason": "I cannot run pytest from a turn",
               "needs": "execution"},
        task_id=_task_id_for(role)))
    assert not isinstance(out, TurnParseError), (role, out)
    assert out.intent.reason == "other"
    assert out.intent.detail == "I cannot run pytest from a turn"
    assert out.intent.needs is not None and out.intent.needs.capability == "execution"


@pytest.mark.parametrize("role", _ROLES)
def test_blocked_turn_with_an_empty_detail_still_fails_closed(role: str) -> None:
    """Always-legal is not the same as always-accepted-empty: a block with
    nothing in it says nothing to the PM, and the schema stays fail-closed."""
    out = parse_coding_turn(role, _task_id_for(role), _envelope(
        role, {"kind": "blocked", "detail": "   "}, task_id=_task_id_for(role)))
    assert isinstance(out, TurnParseError)
    assert out.code == TurnErrorCode.turn_schema_mismatch


def test_blocked_wins_over_the_dev_context_request_relabelling() -> None:
    """F127 relabels an unknown DEV kind carrying a ``question`` into a
    ``context_request``. ``blocked`` is a KNOWN kind and must not be swallowed by
    that path — an exhausted dev's block would otherwise be re-routed straight
    back into the channel it just ran out of."""
    out = parse_coding_turn("dev", "t-1", _envelope(
        "dev", {"kind": "blocked", "detail": "no repo read",
                "question": "what is in hud.py?"}, task_id="t-1"))
    assert not isinstance(out, TurnParseError)
    assert isinstance(out.intent, BlockedIntent)


# --------------------------------------------------------------------------- #
# 2. THE EXAMPLE TABLE IS VALID — a corrective prompt can never teach a
#    rejected shape (one table, two consumers).
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(("role", "kind"), sorted(sc._MINIMAL_INTENT_EXAMPLES))
def test_every_minimal_example_round_trips(role: str, kind: str) -> None:
    text = minimal_valid_example(role, kind, task_id="t-1")
    out = parse_coding_turn(role, _task_id_for(role), text)
    assert not isinstance(out, TurnParseError), (role, kind, out)


@pytest.mark.parametrize("role", _ROLES)
def test_default_and_escape_examples_round_trip(role: str) -> None:
    for text in (minimal_valid_example(role, task_id="t-1"),
                 blocked_example(role, task_id="t-1")):
        out = parse_coding_turn(role, _task_id_for(role), text)
        assert not isinstance(out, TurnParseError), (role, text, out)


def test_unknown_kind_and_unknown_role_still_yield_something_teachable() -> None:
    """A corrective prompt must never have nothing to show."""
    assert json.loads(minimal_valid_example("dev", "no_such_kind"))["intent"]["kind"] \
        == "tool_plan"
    assert json.loads(minimal_valid_example("architect"))["intent"]["kind"] == "blocked"


# --------------------------------------------------------------------------- #
# 3. THE FOUR HISTORICAL INSTANCES, LOCKED AS REGRESSIONS.
# --------------------------------------------------------------------------- #

def test_lock_1_not_done_with_no_tasks_and_one_decision_parses() -> None:
    """Spec 21's fix, re-locked here because Item 3(b)'s progress rule now
    depends on it: "prune the duplicates, add nothing, not done"."""
    out = parse_coding_turn("pm", None, _envelope("pm", {
        "kind": "plan", "done": False,
        "decisions": [{"title": "Drop the duplicate HUD tasks",
                       "choice": "prune",
                       "rationale": "Two open tasks already cover the HUD."}],
    }, task_id=None))
    assert not isinstance(out, TurnParseError), out
    assert out.intent.tasks == []


def test_lock_2_decision_synonym_parses_as_a_pm_decision() -> None:
    """``{"decision": ..., "rationale": ...}`` — the shape models actually emit —
    killed 3 of 4 PM retries on ``missing decisions[0].title``."""
    dec = PMDecision.model_validate(
        {"decision": "Wait for the open PRs", "rationale": "two in review"})
    assert dec.title == "Wait for the open PRs"
    assert dec.choice  # defaulted, never a validation failure


def test_lock_3_a_blocked_dev_turn_is_task_blocked_and_not_unproductive(
    tmp_errorta_home: Path,
) -> None:
    """The behavioural core of Item 1. Every OTHER dev dead end returns
    ``unproductive=True`` and feeds the F127 ladder — honesty and failure were
    the same signal. A blocked dev now reaches ``task_blocked``: the transition
    that has existed in ``_apply_outcome``/``topology.block_task`` all along and
    that no turn shape could produce."""
    store = _store("sp25dev")
    task = store.add_task(title="wire the score HUD", role=DEV)

    def caller(member: Any, prompt: str) -> str:
        return _envelope("dev", {
            "kind": "blocked", "reason": "missing_capability",
            "detail": "I cannot see src/hud.py and have no read tool.",
            "needs": {"capability": "repo_read", "what": "a way to read src/hud.py"},
        }, task_id=task.task_id)

    rt = build_run_turn(store, _real_ws("sp25dev", store),
                        members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    outcome = rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)

    assert outcome.kind == "task_blocked"
    assert outcome.unproductive is False          # THE change: not F127 traffic
    assert outcome.task is not None and outcome.task.task_id == task.task_id
    assert "missing_capability" in outcome.reason
    assert "src/hud.py" in outcome.reason
    assert store.list_prs() == []
    # The typed ask is recorded for the PM (Item 2), separately from the block.
    asks = [d for d in store.list_decisions() if d["choice"] == "capability_ask"]
    assert len(asks) == 1 and "repo_read" in asks[0]["title"]


def test_lock_3b_the_block_lands_on_the_task_and_resets_the_pm_counters() -> None:
    """The other half: ``_apply_outcome`` marks the task blocked with the agent's
    own words and treats it as motion (``pm_idle``/``plan_streak`` reset)."""
    recorded: list[tuple[str, str]] = []

    class Rec:
        def block_task(self, task: Any, *, reason: str) -> None:
            recorded.append((task.task_id, reason))

    class Task:
        task_id, title, state = "t-9", "impl", "doing"

    c = LoopCounters(pm_idle=2, plan_streak=4)
    _apply_outcome(Rec(), object(), object(),  # type: ignore[arg-type]
                   TurnOutcome(kind="task_blocked", task=Task(),
                               reason="missing_capability: no gate"), c)
    assert recorded == [("t-9", "missing_capability: no gate")]
    assert c.pm_idle == 0 and c.plan_streak == 0


def _blocked_worker_run(pid: str, role: str) -> tuple[LedgerStore, Any]:
    """Open a real PR with one dev turn, then answer the REVIEW or TEST task with
    a blocked turn. Returns the store and the blocked turn's outcome."""
    store = _store(pid)
    ws = _real_ws(pid, store)
    dev_task = store.add_task(title="implement add", role=DEV)

    def dev_caller(member: Any, prompt: str) -> str:
        return _envelope("dev", {
            "kind": "tool_plan", "task_type": "implementation",
            "tool_calls": [{"tool": "code_write",
                            "args": {"path": "calc.py",
                                     "content": "def add(a, b):\n    return a + b\n"}}],
        }, task_id=dev_task.task_id)

    rt = build_run_turn(store, ws, members_by_coding_role(MEMBERS), dev_caller,
                        guardrail_enabled=True)
    assert rt(Assign(member_id="m-dev", task_id=dev_task.task_id, role=DEV),
              store).kind == "pr_opened"

    if role == TESTER:
        pr = store.list_prs()[0]
        worker_task = store.add_task(title="test PR", role=TESTER,
                                     pr_id=pr["pr_id"])
        member_id = "m-test"
    else:
        worker_task = [t for t in store.list_tasks() if t.role == REVIEWER][0]
        member_id = "m-rev"

    def blocked_caller(member: Any, prompt: str) -> str:
        return _envelope(role, {
            "kind": "blocked", "reason": "missing_context",
            "detail": "The diff I was given is empty; I cannot judge this PR.",
        }, task_id=worker_task.task_id)

    rt2 = build_run_turn(store, ws, members_by_coding_role(MEMBERS),
                         blocked_caller, guardrail_enabled=True)
    return store, rt2(Assign(member_id=member_id, task_id=worker_task.task_id,
                             role=role), store)


def test_a_blocked_reviewer_blocks_the_review_task_and_fabricates_no_verdict(
    tmp_errorta_home: Path,
) -> None:
    """A reviewer that cannot review must not be forced to invent a verdict.
    The review task blocks; the PR is left exactly as it was."""
    store, outcome = _blocked_worker_run("sp25rev", REVIEWER)
    assert outcome.kind == "task_blocked"
    assert outcome.unproductive is False
    assert "missing_context" in outcome.reason
    pr = store.list_prs()[0]
    assert pr.get("reviewer_approved") is not True
    assert pr["status"] not in ("merged", "changes_requested")


def test_a_blocked_tester_does_not_become_a_failing_test_run(
    tmp_errorta_home: Path,
) -> None:
    """"I cannot answer" must not be converted into "the code is broken" — the
    tester's blocked turn must NOT take the `_changes_requested` path (which
    marks the PR tests-failed and spawns a "fix tests" dev task)."""
    store, outcome = _blocked_worker_run("sp25test", TESTER)
    assert outcome.kind == "task_blocked"
    assert outcome.unproductive is False
    pr = store.list_prs()[0]
    assert pr.get("tests_passed") is not False
    assert not [t for t in store.list_tasks() if t.title.startswith("fix tests")]
    assert not [d for d in store.list_decisions()
                if d["choice"] == "tester_turn_rejected"]


def test_blocking_a_terminal_task_is_a_no_op_with_a_recorded_reason(
    tmp_errorta_home: Path,
) -> None:
    """The concurrent-loop edge: a block that arrives after the task was already
    finished must not regress its state (a `done` task silently re-entering the
    backlog), but the agent's words are still recorded."""
    from errorta_council.coding.topology import CodingReconciler
    store = _store("sp25term")
    task = store.add_task(title="impl", role=DEV)
    store.update_task(task.task_id, state="done")
    task = [t for t in store.list_tasks() if t.task_id == task.task_id][0]

    out = CodingReconciler(store).block_task(task, reason="missing_capability: x")
    assert out.state == "done"
    assert [t for t in store.list_tasks() if t.state == "blocked"] == []
    blocked = [d for d in store.list_decisions() if d["choice"] == "blocked"]
    assert blocked and "missing_capability" in blocked[0]["rationale"]


def test_a_blocked_revise_task_opens_no_pr_and_extends_no_lineage(
    tmp_errorta_home: Path,
) -> None:
    """Spec 16's breaker composes: a block on a `revise:` task opens no PR, so
    the revise chain ENDS rather than deepening — and nothing about the block
    touches the livelock accounting."""
    store = _store("sp25rev2")
    task = store.add_task(title="revise: implement add", role=DEV)

    def caller(member: Any, prompt: str) -> str:
        return _envelope("dev", {
            "kind": "blocked", "reason": "contradictory_instruction",
            "detail": "The review asks for behaviour the spec forbids.",
        }, task_id=task.task_id)

    rt = build_run_turn(store, _real_ws("sp25rev2", store),
                        members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    outcome = rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)
    assert outcome.kind == "task_blocked"
    assert store.list_prs() == []
    assert not [d for d in store.list_decisions()
                if d["choice"] == "revise_chain_broken"]


def test_lock_4_the_exhausted_dev_is_pointed_at_a_field_that_exists(
    tmp_errorta_home: Path,
) -> None:
    """BUG #5, locked before it can cost another run. The exhausted-DEV budget
    text used to end "say so in your summary" — and ``DeveloperToolPlanIntent``
    has no ``summary`` field with ``extra="ignore"``, so that sentence was
    discarded, while the corrective prompt separately said to drop unmodeled
    fields. Nobody wrote that bug; it assembled itself out of two correct
    strings. The text must now name the blocked intent, and must never instruct
    a field the dev schema does not model."""
    store = _store("sp25exh")
    task = store.add_task(title="impl", role=DEV)
    store.record_decision(
        title="context request answered", context=f"task {task.task_id}",
        choice="context_request", rationale="q",
        related_task_ids=[task.task_id],
        extra={"context_response": {"schema_version": "context_response.v1",
                                    "memory": []}})
    store.update_task(task.task_id, context_request_attempts=3)
    task = [t for t in store.list_tasks() if t.task_id == task.task_id][0]

    text = _latest_context_response_text(store, task.task_id, task=task)
    assert "0 remain" in text
    assert '"kind": "blocked"' in text
    assert '"detail"' in text
    # The instruction must not name a field the DEV intent does not model.
    assert '"summary"' not in text
    # And what it teaches must actually parse.
    shape = text.split('{"schema_version"', 1)[1]
    shape = '{"schema_version"' + shape.split("\n", 1)[0]
    assert not isinstance(parse_coding_turn(DEV, task.task_id, shape),
                          TurnParseError)


# --------------------------------------------------------------------------- #
# 4. THE ANTI-DRIFT LOCK — every cross-field validator is satisfied by some
#    example in the table, and the escape shape has none.
# --------------------------------------------------------------------------- #

def _after_validators(model: type[BaseModel]) -> list[str]:
    """Names of the ``model_validator(mode="after")`` methods on ``model``."""
    decorators = model.__pydantic_decorators__.model_validators
    return sorted(name for name, dec in decorators.items()
                  if dec.info.mode == "after")


_INTENT_MODELS: dict[str, type[BaseModel]] = {
    "plan": sc.PMPlanIntent,
    "tool_plan": sc.DeveloperToolPlanIntent,
    "context_request": sc.DeveloperContextRequestIntent,
    "review_verdict": sc.ReviewerVerdictIntent,
    "test_plan": sc.TesterPlanIntent,
    "blocked": sc.BlockedIntent,
}


def test_every_intent_kind_has_at_least_one_satisfying_example() -> None:
    """Enumerate the cross-field validators across the intent models; for each
    kind, assert at least one example in the table parses INTO that model — i.e.
    satisfies every one of its after-validators at once. A new relationship rule
    that no example can satisfy fails here, naming the validator."""
    parsed_kinds: dict[str, list[str]] = {}
    for (role, kind) in sorted(sc._MINIMAL_INTENT_EXAMPLES):
        out = parse_coding_turn(role, _task_id_for(role),
                                minimal_valid_example(role, kind, task_id="t-1"))
        assert not isinstance(out, TurnParseError), (role, kind, out)
        parsed_kinds.setdefault(type(out.intent).__name__, []).append(f"{role}/{kind}")
    for role in _ROLES:
        out = parse_coding_turn(role, _task_id_for(role),
                                blocked_example(role, task_id="t-1"))
        assert not isinstance(out, TurnParseError), (role, out)
        parsed_kinds.setdefault(type(out.intent).__name__, []).append(f"{role}/blocked")

    for kind, model in _INTENT_MODELS.items():
        assert model.__name__ in parsed_kinds, (
            f"no minimal example satisfies {model.__name__} (kind={kind}); its "
            f"cross-field validators are {_after_validators(model)} — add an "
            "example to schemas._MINIMAL_INTENT_EXAMPLES or relax the rule")


def test_the_escape_shape_carries_no_cross_field_rule() -> None:
    """The property that makes ``blocked`` unconditionally legal. Its ONLY
    after-validator is the non-empty ``detail`` check; anything else added here
    is a relationship between fields, and relationships are what made the other
    five intents unsatisfiable in some reachable state."""
    assert _after_validators(sc.BlockedIntent) == ["_detail_required"]
    assert _after_validators(sc.CapabilityAsk) == []
    # And every field except `detail` has a default, so the model is
    # constructible from the detail alone.
    required = [n for n, f in BlockedIntent.model_fields.items() if f.is_required()]
    assert required == ["kind", "detail"]


def test_the_tool_catalog_names_the_escape_shape_for_every_role() -> None:
    for role in _ROLES:
        for repo_read in (False, True):
            for gate in (False, True):
                txt = tool_catalog_text(role, repo_read=repo_read, gate=gate)
                assert '"kind": "blocked"' in txt, (role, repo_read, gate)


# --------------------------------------------------------------------------- #
# 5. ITEM 3 — a shape rejection is not a progress failure (and is bounded).
# --------------------------------------------------------------------------- #

def _apply(outcome: TurnOutcome, c: LoopCounters,
           policy: CodingAutonomyPolicy | None = None) -> None:
    class Rec:
        def block_task(self, task: Any, *, reason: str) -> None:  # pragma: no cover
            pass

    _apply_outcome(Rec(), object(), object(),  # type: ignore[arg-type]
                   outcome, c, None, policy)


def _rejected() -> TurnOutcome:
    return TurnOutcome(kind="planned", made_progress=False, schema_rejected=True)


def test_gravity_golf_replay_moves_schema_rejects_not_pm_idle() -> None:
    """Four PM turns rejected for SHAPE, two PRs open. The run used to stop
    ``no_progress`` at ``pm_idle_limit=2`` — trying to comply accelerated
    termination. Now the counter that moves is ``schema_rejects``."""
    policy = CodingAutonomyPolicy()
    c = LoopCounters()
    for _ in range(3):
        _apply(_rejected(), c, policy)
    assert c.schema_rejects == 3
    assert c.pm_idle == 0 < policy.pm_idle_limit
    assert c.plan_streak == 0      # a turn that never parsed is not a plan


def test_a_parsing_turn_clears_the_shape_rejection_window() -> None:
    """A transient malformed response between good turns costs nothing."""
    c = LoopCounters()
    _apply(_rejected(), c)
    assert c.schema_rejects == 1
    _apply(TurnOutcome(kind="planned", made_progress=True), c)
    assert c.schema_rejects == 0 and c.pm_idle == 0


def test_past_the_limit_shape_rejections_count_as_idle_again() -> None:
    """The bound. A schema the PM genuinely cannot satisfy must still end the
    run — with no new stop reason (batch regression lock 1): the rejections
    resume feeding ``pm_idle`` and the ordinary ``no_progress`` stop lands."""
    policy = CodingAutonomyPolicy(schema_reject_limit=3, pm_idle_limit=2)
    c = LoopCounters()
    for _ in range(3):
        _apply(_rejected(), c, policy)
    assert c.pm_idle == 0
    for i in range(1, 3):
        _apply(_rejected(), c, policy)
        assert c.pm_idle == i
    assert c.pm_idle >= policy.pm_idle_limit


def test_schema_reject_limit_zero_reproduces_todays_accounting() -> None:
    """Every knob at its disable value reproduces today's trace (lock 2)."""
    policy = CodingAutonomyPolicy(schema_reject_limit=0)
    c = LoopCounters()
    _apply(_rejected(), c, policy)
    assert c.pm_idle == 1 and c.plan_streak == 1


def test_lock_6_empty_parsing_turns_still_stop_at_pm_idle_limit() -> None:
    """Regression lock 6 of the batch plan: expressibility must not become a
    licence to idle. Turns that PARSE and do nothing are unaffected."""
    policy = CodingAutonomyPolicy(pm_idle_limit=2)
    c = LoopCounters()
    for _ in range(4):
        _apply(TurnOutcome(kind="planned", made_progress=False), c, policy)
    assert c.pm_idle == 4 >= policy.pm_idle_limit
    assert c.schema_rejects == 0


def test_schema_reject_limit_survives_the_policy_round_trip() -> None:
    """The F145 canary: a knob that does not round-trip is a knob an operator
    cannot set."""
    p = CodingAutonomyPolicy(schema_reject_limit=7)
    assert policy_to_dict(p)["schema_reject_limit"] == 7
    assert policy_from_dict(policy_to_dict(p)).schema_reject_limit == 7
    assert policy_from_dict({}).schema_reject_limit == \
        CodingAutonomyPolicy().schema_reject_limit
    assert policy_from_dict({"schema_reject_limit": -4}).schema_reject_limit == 0


def test_a_novel_decision_only_turn_is_progress_and_a_repeat_is_not() -> None:
    """Item 3(b). The decision-only turn Spec 21 legalised counts as progress —
    but only when it recorded something new, else "explain yourself" becomes a
    licence to idle forever."""
    class Intent:
        def __init__(self, titles: list[str], tasks: list[Any] | None = None) -> None:
            self.decisions = [type("D", (), {"title": t})() for t in titles]
            self.tasks = tasks or []

    assert _pm_turn_made_progress(Intent(["Wait on the HUD PR"]), [], set()) is True
    assert _pm_turn_made_progress(
        Intent(["Wait on the HUD PR"]), [], {"Wait on the HUD PR"}) is False
    # Spec 08 is untouched: an all-duplicate batch is churn, decisions or not.
    assert _pm_turn_made_progress(
        Intent(["a new decision"], tasks=[object()]), [], set()) is False
    # And a genuinely empty turn is still no-progress (lock 6).
    assert _pm_turn_made_progress(Intent([]), [], set()) is False


def test_a_rejected_pm_turn_reports_schema_rejected(tmp_errorta_home: Path) -> None:
    """The runner half of the split: the PM rejection path sets the flag (and
    still records the raw validator dump for the operator)."""
    from errorta_council.coding.topology import Plan
    store = _store("sp25rej")

    def caller(member: Any, prompt: str) -> str:
        return "not json at all"

    rt = build_run_turn(store, None, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    outcome = rt(Plan(member_id="m-pm"), store)
    assert outcome.kind == "planned"
    assert outcome.made_progress is False
    assert outcome.schema_rejected is True
    assert any(d["choice"] == "pm_turn_rejected" for d in store.list_decisions())


# --------------------------------------------------------------------------- #
# 6. THE ESCAPE SHAPE IS BOUNDED — it cannot become a way to idle forever.
# --------------------------------------------------------------------------- #

def _blocked_outcome() -> TurnOutcome:
    return TurnOutcome(kind="task_blocked", member_id="m-dev", member_role=DEV,
                       reason="missing_capability: no gate")


def test_blocked_turn_limit_routes_the_task_to_the_recovery_ladder() -> None:
    policy = CodingAutonomyPolicy(blocked_turn_limit=3)
    c = LoopCounters()
    action = Assign(member_id="m-dev", task_id="t-1", role=DEV)
    for i in (1, 2):
        outcome = _blocked_outcome()
        _account_blocked_turn(c, policy, action, outcome)
        assert outcome.unproductive is False, i
    outcome = _blocked_outcome()
    _account_blocked_turn(c, policy, action, outcome)
    assert outcome.unproductive is True          # F127 ladder takes it from here
    assert outcome.reason.startswith("blocked_turn_limit: ")
    assert c.blocked_counts[("m-dev", "t-1")] == 3


def test_blocked_turn_limit_is_per_member_and_task() -> None:
    policy = CodingAutonomyPolicy(blocked_turn_limit=2)
    c = LoopCounters()
    for task_id in ("t-1", "t-2"):
        outcome = _blocked_outcome()
        _account_blocked_turn(
            c, policy, Assign(member_id="m-dev", task_id=task_id, role=DEV), outcome)
        assert outcome.unproductive is False


def test_blocked_turn_limit_zero_disables_the_accounting() -> None:
    policy = CodingAutonomyPolicy(blocked_turn_limit=0)
    c = LoopCounters()
    action = Assign(member_id="m-dev", task_id="t-1", role=DEV)
    for _ in range(10):
        outcome = _blocked_outcome()
        _account_blocked_turn(c, policy, action, outcome)
        assert outcome.unproductive is False
    assert c.blocked_counts == {}


def test_a_non_blocked_outcome_is_untouched_by_the_accounting() -> None:
    c = LoopCounters()
    outcome = TurnOutcome(kind="noop")
    _account_blocked_turn(c, CodingAutonomyPolicy(),
                          Assign(member_id="m-dev", task_id="t-1", role=DEV), outcome)
    assert outcome.unproductive is False and c.blocked_counts == {}


# --------------------------------------------------------------------------- #
# 7. ITEM 4 — the corrective prompt teaches the accepted shape.
# --------------------------------------------------------------------------- #

_PM_DONE_RULE_DUMP = (
    "invalid pm intent: [{'type': 'value_error', 'loc': (), 'msg': 'Value error, "
    "done=false requires at least one task or decision', 'input': {'kind': 'plan'}, "
    "'url': 'https://errors.pydantic.dev/2.11/v/value_error'}]")


def test_corrective_prompt_teaches_a_valid_example_and_the_escape_shape() -> None:
    out = _corrective_turn_prompt(
        "ORIGINAL", TurnParseError(TurnErrorCode.turn_schema_mismatch,
                                   _PM_DONE_RULE_DUMP),
        retry=1, max_retries=1, role=PM, task_id=None)
    assert "ORIGINAL" in out
    # (1) what was wrong, in plain language
    assert "done=false requires at least one task or decision" in out
    # (2) a minimal VALID envelope for this seat, which really parses
    example = minimal_valid_example(PM)
    assert example in out
    assert not isinstance(parse_coding_turn(PM, None, example), TurnParseError)
    # (3) the escape shape, named as always accepted
    assert blocked_example(PM) in out
    assert "ALWAYS accepted" in out


def test_corrective_prompt_carries_no_pydantic_internals() -> None:
    out = _corrective_turn_prompt(
        "ORIGINAL", TurnParseError(TurnErrorCode.turn_schema_mismatch,
                                   _PM_DONE_RULE_DUMP),
        retry=1, max_retries=1, role=PM, task_id=None)
    for internal in ("pydantic.dev", "'loc':", "'type':", "'input':", "'url':"):
        assert internal not in out, internal


def test_humanized_detail_names_the_field_that_was_missing() -> None:
    dump = ("invalid pm intent: [{'type': 'missing', 'loc': ('decisions', 0, "
            "'title'), 'msg': 'Field required', 'input': {}, "
            "'url': 'https://errors.pydantic.dev/2.11/v/missing'}]")
    text = _humanize_parse_detail("turn_schema_mismatch", dump)
    assert "decisions[0].title: Field required" in text
    assert "pydantic" not in text


def test_humanized_detail_degrades_to_a_plain_sentence() -> None:
    assert _humanize_parse_detail(
        "turn_non_json", "no parseable JSON object") == \
        "your response contained no JSON envelope"


def test_the_raw_dump_is_still_recoverable_from_the_ledger(
    tmp_errorta_home: Path,
) -> None:
    """The Pydantic dump does not disappear — it moves to where an operator can
    read it and a model cannot be confused by it."""
    store = _store("sp25dump")
    task = store.add_task(title="impl", role=DEV)

    def caller(member: Any, prompt: str) -> str:
        return _envelope("dev", {"kind": "tool_plan",
                                 "task_type": "implementation",
                                 "tool_calls": []}, task_id=task.task_id)

    rt = build_run_turn(store, _real_ws("sp25dump", store),
                        members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)
    retries = [d for d in store.list_decisions()
               if d["choice"] == "dev_turn_correction_retry"]
    assert retries and "'loc':" in retries[0]["rationale"]


def test_corrective_prompt_without_a_role_still_works() -> None:
    """Defensive: the teaching block is additive, never a precondition."""
    out = _corrective_turn_prompt(
        "ORIGINAL", TurnParseError(TurnErrorCode.turn_non_json, "no JSON"),
        retry=1, max_retries=2)
    assert "coding_turn.v1" in out


# --------------------------------------------------------------------------- #
# 8. NOTHING ELSE MOVED — every existing turn shape parses unchanged.
# --------------------------------------------------------------------------- #

def test_existing_turn_shapes_are_unchanged() -> None:
    cases = [
        ("pm", None, {"kind": "plan", "done": False,
                      "tasks": [{"title": "t", "role": "dev"}]}),
        ("dev", "t-1", {"kind": "tool_plan", "task_type": "implementation",
                        "tool_calls": [{"tool": "code_write",
                                        "args": {"path": "a.py", "content": "x"}}]}),
        ("dev", "t-1", {"kind": "context_request", "question": "what?"}),
        ("reviewer", "t-1", {"kind": "review_verdict", "reviewed_head": "abc",
                             "approved": True}),
        ("tester", "t-1", {"kind": "test_plan", "command_ids": ["c1"],
                           "scope": "changed_files"}),
    ]
    for role, task_id, intent in cases:
        out = parse_coding_turn(role, task_id, _envelope(role, intent,
                                                         task_id=task_id))
        assert not isinstance(out, TurnParseError), (role, intent, out)
        assert not isinstance(out.intent, BlockedIntent)


def test_capability_ask_is_optional_and_never_grants_anything() -> None:
    """The non-goal, in code: an ask is a REQUEST recorded for a human-or-PM
    decision. Nothing in this spec mutates the role→tool table."""
    from errorta_council.coding.turn_controller import allowed_tools_for_role
    ask = CapabilityAsk(capability="execution", what="a way to run pytest")
    assert ask.capability == "execution"
    assert allowed_tools_for_role(DEV) == ("code_write", "code_edit")
    for role in (PM, REVIEWER, TESTER):
        assert allowed_tools_for_role(role) == ()


# --------------------------------------------------------------------------- #
# 9. Item 2's ANSWERABLE half — the recorded ask REACHES the PM.
# --------------------------------------------------------------------------- #
#
# `_record_capability_ask` shipped without a reader. The only consumers of a
# `capability_ask` decision were the writer itself, the team-log UI renderer
# (`team_log.py:188` — not part of any prompt), and the test above. So a worker
# asked for a capability and the PM never saw the ask: unanswerable by
# construction, which is exactly the class this spec exists to close. These lock
# the DELIVERY, not the serialization.

def _blocked_with_needs(capability: str, what: str, why: str = "") -> BlockedIntent:
    return BlockedIntent(
        kind="blocked", reason="missing_capability", detail="cannot proceed",
        needs=CapabilityAsk(capability=capability, what=what, why=why))


def test_capability_ask_note_is_empty_with_no_asks(tmp_errorta_home) -> None:
    from errorta_council.coding import runner
    assert runner._capability_ask_note(_store("s25-ask0")) == ""


def test_a_dev_ask_reaches_the_pm_prompt(tmp_errorta_home) -> None:
    """THE gap. A DEV records a capability ask; the PM's very next composed prompt
    carries the NOTE — naming the role, the capability, and the worker's own words.

    The prompt assertion is deliberately keyed on the note's own instruction text,
    NOT on "execution" or "run pytest". Those two strings are already in the prompt
    without this change: ``ensure_pm_working_memory`` dumps a ``recent_decisions``
    JSON blob that includes the raw ``capability_ask`` record. A recency-bounded
    raw dump with no instruction attached is not delivery — a test that asserted on
    those substrings would have gone green against the unwired build."""
    from errorta_council.coding import runner
    s = _store("s25-ask1")
    task = s.add_task(role="dev", title="Add a gravity solver")
    runner._record_capability_ask(
        s, _blocked_with_needs("execution", "a way to run pytest and see output"),
        role=DEV, task=task, context=f"task {task.task_id}")
    assert [d for d in s.list_decisions() if d["choice"] == "capability_ask"]

    note = runner._capability_ask_note(s)
    assert "dev" in note and "execution" in note
    assert "run pytest" in note

    prompt = runner._pm_prompt(s)
    assert note in prompt
    assert "open and unanswered" in prompt and "CANNOT grant a tool" in prompt


def test_the_note_tells_the_pm_it_cannot_grant_and_what_it_can_do(
        tmp_errorta_home) -> None:
    """An ask the PM cannot answer is no better than an ask it never sees. The
    note carries the answer shapes (re-scope / register a test command / split /
    cancel) and states that the PM CANNOT grant a tool — the spec's non-goal, in
    the prompt rather than only in a comment."""
    from errorta_council.coding import runner
    s = _store("s25-ask2")
    task = s.add_task(role="dev", title="Run the acceptance gate")
    runner._record_capability_ask(
        s, _blocked_with_needs("execution", "run the tests"),
        role=DEV, task=task, context=f"task {task.task_id}")
    note = runner._capability_ask_note(s).lower()
    assert "cannot grant" in note
    assert "test command" in note and "cancel_task_ids" in note


def test_one_role_capability_pair_is_one_line_however_many_asks(
        tmp_errorta_home) -> None:
    """Dedupe discipline, same as ``_capability_gap_note``: a dev that asks the
    same thing on five tasks must not write five lines into the PM prompt."""
    from errorta_council.coding import runner
    s = _store("s25-ask3")
    for i in range(5):
        task = s.add_task(role="dev", title=f"task {i}")
        runner._record_capability_ask(
            s, _blocked_with_needs("execution", "run the tests"),
            role=DEV, task=task, context=f"task {task.task_id}")
    note = runner._capability_ask_note(s)
    assert note.count("asked for") == 1
    assert note.startswith("1 capability ask")


def test_the_note_clears_itself_when_the_asking_task_settles(
        tmp_errorta_home) -> None:
    """Sibling behaviour of ``_duplicate_rejection_note``: the note is about LIVE
    work. Once the asking task is dropped there is nothing for the PM to answer,
    and a stale ask nagging forever is how a prompt fills with settled history."""
    from errorta_council.coding import runner
    s = _store("s25-ask4")
    task = s.add_task(role="dev", title="Add a gravity solver")
    runner._record_capability_ask(
        s, _blocked_with_needs("repo_read", "read the existing engine"),
        role=DEV, task=task, context=f"task {task.task_id}")
    assert runner._capability_ask_note(s) != ""
    s.update_task(task.task_id, state="dropped")
    assert runner._capability_ask_note(s) == ""


def test_a_blocked_tasks_ask_still_stands(tmp_errorta_home) -> None:
    """The regression the OPEN_STATES shortcut would have shipped: a DEV's blocked
    turn moves its task to ``blocked``, which is NOT in ``task_dedupe.OPEN_STATES``.
    Filtering on that set would hide every ask the moment it was made."""
    from errorta_council.coding import runner
    s = _store("s25-ask4b")
    task = s.add_task(role="dev", title="Add a gravity solver")
    runner._record_capability_ask(
        s, _blocked_with_needs("execution", "run the tests"),
        role=DEV, task=task, context=f"task {task.task_id}")
    s.update_task(task.task_id, state="blocked")
    assert "execution" in runner._capability_ask_note(s)


def test_a_run_level_ask_with_no_task_still_stands(tmp_errorta_home) -> None:
    """The PM's own ask carries no ``related_task_ids``; nothing can settle it, so
    it must not be filtered out by the still-live rule."""
    from errorta_council.coding import runner
    s = _store("s25-ask5")
    runner._record_capability_ask(
        s, _blocked_with_needs("other", "a human decision on scope"),
        role=PM, task=None, context="plan")
    assert "other" in runner._capability_ask_note(s)


def test_the_note_is_failure_tolerant() -> None:
    """Same discipline as every sibling note: prompt assembly must never raise."""
    from errorta_council.coding import runner

    class _Broken:
        def list_decisions(self):
            raise RuntimeError("ledger hiccup")

        def list_tasks(self):
            return []

    assert runner._capability_ask_note(_Broken()) == ""


def test_surfacing_an_ask_to_the_pm_grants_nothing(tmp_errorta_home) -> None:
    """The delivery half must not weaken the non-goal above: the role→tool table
    is identical before and after the ask is recorded AND surfaced."""
    from errorta_council.coding import runner
    from errorta_council.coding.turn_controller import allowed_tools_for_role
    s = _store("s25-ask6")
    task = s.add_task(role="dev", title="Run the acceptance gate")
    runner._record_capability_ask(
        s, _blocked_with_needs("execution", "run the tests"),
        role=DEV, task=task, context=f"task {task.task_id}")
    assert runner._capability_ask_note(s) != ""
    assert allowed_tools_for_role(DEV) == ("code_write", "code_edit")
    for role in (PM, REVIEWER, TESTER):
        assert allowed_tools_for_role(role) == ()
