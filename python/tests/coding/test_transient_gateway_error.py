"""A transient decode/network failure from a member's model call must degrade to
a member failure, not crash the whole run.

Repro of the original defect: project ``pixel-creatures`` recorded
``run_state.status="failed"`` with ``last_error="error: Error -3 while
decompressing data: incorrect header check"`` ~141ms after Start. That is a raw
``zlib.error`` (``zlib.error.__name__ == "error"``) surfaced from a flaky model
backend. These lock the two-layer defense:

1. A member caller that raises a raw ``zlib.error`` degrades through the F120
   member-health ladder to a clean terminal ``member_unhealthy`` — NOT an
   uncaught crash / budget runaway.
2. The top-level worker's ``_is_transient_gateway_error`` classifier recognizes
   transient wire failures (so the route records a clean, recoverable state)
   while leaving genuine defects on the raw-message path.
"""
from __future__ import annotations

import concurrent.futures
import zlib
from pathlib import Path

import httpx
import pytest

from errorta_app.routes.coding import _is_transient_gateway_error
from errorta_council.coding.autonomy import (
    CADENCE_OFF,
    MEMBER_UNHEALTHY,
    CodingAutonomyPolicy,
    run_coding_loop,
)
from errorta_council.coding.governance import GovernanceState, GovernanceStore
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import build_run_turn, members_by_coding_role
from errorta_council.gateway_local import RetryableError

_ZLIB_MSG = "Error -3 while decompressing data: incorrect header check"

_MEMBERS = [
    {"id": "m-1", "enabled": True, "metadata": {"coding_role": "pm"},
     "gateway_route_id": "claude_cli.opus", "provider_kind": "claude_cli"},
    {"id": "m-2", "enabled": True, "metadata": {"coding_role": "dev"},
     "gateway_route_id": "cursor_cli.composer-2.5", "provider_kind": "cursor_cli"},
]
_PAIRS = [(m["id"], m["metadata"]["coding_role"]) for m in _MEMBERS]


def _governed_store(tmp_path: Path) -> LedgerStore:
    s = LedgerStore("tge", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    GovernanceStore.for_ledger(s).save_state(
        GovernanceState(mode="light", phase="brainstorming",
                        block_on_problems=True))
    return s


@pytest.mark.parametrize("max_parallel_workers", [1, 2])
def test_member_zlib_error_degrades_not_crash(
    tmp_path: Path, max_parallel_workers: int,
) -> None:
    """A member call raising a raw ``zlib.error`` must not escape the run loop —
    it degrades to a terminal ``member_unhealthy`` within the failure cap."""
    store = _governed_store(tmp_path)

    def caller(member, prompt):  # noqa: ANN001, ARG001
        raise zlib.error(_ZLIB_MSG)

    rt = build_run_turn(store, None, members_by_coding_role(_MEMBERS), caller,
                        guardrail_enabled=False)
    # If the exception ever escaped, this call would raise instead of returning.
    res = run_coding_loop(
        store, _PAIRS,
        CodingAutonomyPolicy(
            checkpoint_cadence=CADENCE_OFF, member_failure_limit=3,
            max_iterations=200, max_parallel_workers=max_parallel_workers),
        run_turn=rt,
    )
    assert res.stop_reason == MEMBER_UNHEALTHY
    # Transient (errored) reason caps at member_failure_limit, not hundreds.
    assert res.counters.iterations < 100


def test_transient_classifier_recognizes_wire_failures() -> None:
    assert _is_transient_gateway_error(zlib.error(_ZLIB_MSG)) is True
    assert _is_transient_gateway_error(httpx.DecodingError("bad")) is True
    # ConnectError is an httpx.TransportError subclass.
    assert _is_transient_gateway_error(httpx.ConnectError("refused")) is True
    assert _is_transient_gateway_error(
        concurrent.futures.TimeoutError()) is True
    assert _is_transient_gateway_error(
        RetryableError("gateway_decode_error: error")) is True


def test_transient_classifier_leaves_real_defects_alone() -> None:
    """Genuine bugs must stay on the raw ``type: message`` path so they remain
    debuggable — never masked as a transient hiccup."""
    assert _is_transient_gateway_error(ValueError("bad state")) is False
    assert _is_transient_gateway_error(KeyError("id")) is False
    assert _is_transient_gateway_error(RuntimeError("boom")) is False
