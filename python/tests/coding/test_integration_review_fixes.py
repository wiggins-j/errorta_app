"""Cross-branch interaction defects found by an integration review.

Four branches were developed in isolated worktrees, each green on its own suite.
These are the defects that existed only in the COMBINATION — no individual author
could have seen them, and no individual suite could have caught them.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.autonomy import (
    CodingAutonomyPolicy,
    LoopCounters,
    TurnOutcome,
    _handle_unproductive,
    _progress_fingerprint,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import DEV, Assign


def _store(tmp_path: Path, name: str = "integ") -> LedgerStore:
    s = LedgerStore(name, root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def _unproductive_turn(store, task_id, counters, policy):
    action = Assign(member_id="m-1", task_id=task_id, role=DEV)
    outcome = TurnOutcome(kind="noop", unproductive=True, member_id="m-1",
                          member_role=DEV, member_route="local.qwen3.5:9b",
                          reason="unusable_output")
    return _handle_unproductive(store, action, outcome, counters, policy,
                                [("m-1", DEV)])


# --------------------------------------------------------------------------- #
# IMPORTANT 1 — disabling the F127 ladder must not also disable its backstop
# --------------------------------------------------------------------------- #

def test_disabled_ladder_does_not_pollute_the_progress_fingerprint(
        tmp_path: Path) -> None:
    """`worker_unproductive_limit=0` removed the ONLY detector for unusable-output
    churn AND structurally blocked the backstop meant to cover it.

    `sum(unproductive_counts.values())` is a term in `_progress_fingerprint`'s
    ladder tuple. That counter used to oscillate because every rung reset it — but
    with the ladder disabled the function returned BEFORE those resets while still
    incrementing, so the sum grew monotonically and the fingerprint differed on
    every unproductive turn. `_account_convergence` re-anchors `last_progress_iter`
    whenever the fingerprint changes, so `not_converging` could never trip.

    `_progress_fingerprint`'s own docstring delegates this exact pathology to the
    F127 ladder — so the combination removed the owner and the backstop at once,
    and a run on a weak model would burn to `budget_exhausted`, reporting a budget
    problem instead of the real one.
    """
    store = _store(tmp_path)
    task = store.add_task(title="t", role=DEV)
    policy = CodingAutonomyPolicy(worker_unproductive_limit=0)
    c = LoopCounters()

    prints = set()
    for _ in range(6):
        _unproductive_turn(store, task.task_id, c, policy)
        prints.add(_progress_fingerprint(store, c))

    assert len(prints) == 1, (
        f"a disabled ladder must contribute NOTHING to the fingerprint; got "
        f"{len(prints)} distinct prints over 6 identical unproductive turns")
    assert sum(c.unproductive_counts.values()) == 0


def test_enabled_ladder_still_counts(tmp_path: Path) -> None:
    # The both-sides control: moving the increment below the disable check must
    # not stop the enabled ladder from counting.
    store = _store(tmp_path)
    task = store.add_task(title="t", role=DEV)
    policy = CodingAutonomyPolicy(worker_unproductive_limit=2)
    c = LoopCounters()

    _unproductive_turn(store, task.task_id, c, policy)
    assert sum(c.unproductive_counts.values()) == 1
