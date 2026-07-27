"""Spec 28 (Item 7) — Tier 2: the LIVE acceptance smoke run. Never gating.

The same north star and definition of done as the Tier 1 fixture
(``test_spec28_autonomy_acceptance.py``), but against real members on real routes,
with the real ``_default_node_runner`` (real Playwright, real Chromium) and a real
workspace. Tier 1 proves the HARNESS can carry a healthy team to a finished
product; this is the only thing that says anything at all about whether a real team
behaves like one — and it says it weakly, advisorily, and expensively.

Marker discipline, read against ``pyproject.toml``'s declarations:

* ``live``   — "needs real models/network; non-gating (nightly/manual opt-in)".
* ``manual`` — "not fully automatable; tracked for human release sign-off". With no
  CI, "nightly" is a maintainer's local schedule and the release signal is a human
  confirming this passed.
* ``blocking`` is deliberately NOT applied: a live run that fails because a provider
  is down or a key expired must not hold a release.
* ``smoke`` is not applied either — "fast, runs on every push" is its opposite.

``pyproject.toml``'s ``addopts`` deselects ``live``/``flaky``/``manual``, so this
file is invisible to ``( cd python && pytest )``. Running it takes BOTH an explicit
marker selection and ``ERRORTA_LIVE_ACCEPTANCE=1``; ``scripts/live-acceptance.sh``
does both and prints the stop reason plus the usage rollup afterwards.

COST, stated honestly. The 2026-07-24 run is the reference for unbounded: 96 PRs
over 3h20m. This tier is capped by the engine's own hard budget rather than by hope
— ``max_model_calls=120`` (enforced before dispatch by ``reserve_model_calls``,
including for the concurrent loop), ``max_iterations=60``, and a 45-minute
wall-clock cap via ``should_cancel``. At 120 frontier calls with coding-sized
prompts, expect single-digit to low-double-digit dollars and 20-45 minutes. The
ENFORCED figure is the call cap, which is exact; the dollar figure is a translation
of it and should be re-derived from the run's own usage rollup.

CADENCE: weekly, and mandatory within 7 days of a release cut.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

import pytest

from errorta_council.coding import autonomy, web_probe
from errorta_council.coding.autonomy import (
    CADENCE_OFF,
    DEFINITION_OF_DONE,
    CodingAutonomyPolicy,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import CodingRunner, gateway_member_caller

from .test_spec28_autonomy_acceptance import (  # noqa: F401 - shared fixture contract
    DOD,
    NORTH_STAR,
    RunFixture,
    _assert_artifact_graph_on_master,
    _playwright_available,
)

pytestmark = [pytest.mark.live, pytest.mark.acceptance, pytest.mark.e2e,
              pytest.mark.manual]

_ENV_GUARD = "ERRORTA_LIVE_ACCEPTANCE"
_MAX_MODEL_CALLS = 120
_MAX_ITERATIONS = 60
_WALL_CLOCK_S = 45 * 60


def _live_members() -> list[dict[str, Any]]:
    """The seated council for a live run, read from the operator's own Council
    config. Returns ``[]`` when nothing live is configured, which SKIPS: ``-m live``
    on a laptop must never start spending by accident."""
    try:
        from errorta_council.store import CouncilStore  # type: ignore
    except Exception:  # noqa: BLE001 — no council module -> nothing to run against
        return []
    try:
        members = [m for m in CouncilStore().list_members()  # type: ignore[call-arg]
                   if m.get("enabled", True) and m.get("gateway_route_id")]
    except Exception:  # noqa: BLE001
        return []
    roles = {str((m.get("metadata") or {}).get("coding_role") or "") for m in members}
    return members if {"pm", "dev", "reviewer"} <= roles else []


def _live_caller() -> Optional[Any]:
    try:
        from errorta_council.gateway import Gateway  # type: ignore

        return gateway_member_caller(Gateway())
    except Exception:  # noqa: BLE001 — no gateway -> skip rather than fail
        return None


@pytest.mark.skipif(os.environ.get(_ENV_GUARD) != "1",
                    reason=f"set {_ENV_GUARD}=1 to spend real money on a live run")
@pytest.mark.skipif(not _playwright_available(),
                    reason="a live acceptance run without a browser is not a smoke "
                           "test — install node + Playwright + Chromium")
def test_live_acceptance_run_reaches_definition_of_done(tmp_path) -> None:
    members = _live_members()
    caller = _live_caller()
    if not members or caller is None:
        pytest.skip("no live council/gateway route is configured")

    project_id = f"spec28-live-{int(time.time())}"
    store = LedgerStore(project_id)
    store.create_project(north_star=NORTH_STAR, definition_of_done=DOD,
                         target="new", repo_path=None)
    policy = CodingAutonomyPolicy(
        checkpoint_cadence=CADENCE_OFF,
        max_iterations=_MAX_ITERATIONS,
        max_model_calls=_MAX_MODEL_CALLS,
        gate_min_merge_interval=1,
    )
    autonomy.save_policy(store, policy)

    deadline = time.monotonic() + _WALL_CLOCK_S
    runner = CodingRunner(project_id, members, caller, guardrail_enabled=True)
    res = runner.run(policy, should_cancel=lambda: time.monotonic() > deadline)

    fx = RunFixture(store, runner, team=None, counters=None, result=res)  # type: ignore[arg-type]
    # B1 plus Item 2 assertions 3-4. The STRICT Item 5 budget stays in Tier 1, where
    # it is meaningful: a live PM will genuinely need revises and context requests,
    # and a band tuned on a scripted transcript would only manufacture false alarms.
    assert res.stop_reason == DEFINITION_OF_DONE, (res.stop_reason, res.detail)
    assert store.get_project().status == "done"
    _assert_artifact_graph_on_master(fx)
    delivered = fx.delivered_head()
    probes = [r for r in fx.probe_runs() if str(r.get("head") or "") == delivered]
    assert probes, [r.get("head") for r in fx.probe_runs()]
    assert all(r["passed"] for r in probes), probes
    assert probes[-1]["command_ids"] == [web_probe.PROBE_COMMAND_ID]
