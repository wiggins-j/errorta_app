"""F037 admission matrix — exhaustive config permutations of the pure
evaluator. No I/O. Proves disabled/exhausted/unknown-route/unauthorized
requests never resolve to "admit" (so the scheduler never gateway-calls them).
"""
from __future__ import annotations

import pytest

from errorta_council.callouts.admission import evaluate_callout
from errorta_council.schema import EscalationPolicy, EscalationRosterEntry


def _target(**over) -> EscalationRosterEntry:
    base = dict(id="t", gateway_route_id="fake.local.x", provider_kind="local")
    base.update(over)
    return EscalationRosterEntry(**base)


def _pol(**over) -> EscalationPolicy:
    base = dict(enabled=True, approval_mode="auto", max_callouts_per_run=2,
                max_remote_callouts_per_run=1,
                require_user_approval_before_first_remote_callout=True)
    base.update(over)
    return EscalationPolicy(**base)


def _ev(**over):
    base = dict(
        policy=_pol(), target=_target(), requester_type="user",
        callouts_done=0, remote_callouts_done=0, route_kind="local",
        run_terminal=False,
    )
    base.update(over)
    return evaluate_callout(**base)


# --- reject paths (must NEVER admit) --------------------------------------

def test_disabled_policy_rejects():
    d = _ev(policy=_pol(enabled=False))
    assert d.rejected and d.reason_code == "escalation_disabled"


def test_terminal_run_rejects():
    d = _ev(run_terminal=True)
    assert d.rejected and d.reason_code == "run_terminal"


def test_unknown_target_rejects():
    d = _ev(target=None)
    assert d.rejected and d.reason_code == "unknown_callout_target"


def test_approval_mode_disabled_is_hard_reject():
    d = _ev(policy=_pol(approval_mode="disabled"))
    assert d.rejected and d.reason_code == "approval_disabled"


def test_member_requester_rejected_in_user_only_mode():
    d = _ev(policy=_pol(requester_mode="user_only"), requester_type="member")
    assert d.rejected and d.reason_code == "requester_not_allowed"


def test_callout_count_cap_exhausted_rejects():
    d = _ev(callouts_done=2)  # cap is 2
    assert d.rejected and d.reason_code == "callout_budget_exhausted"


def test_remote_callout_cap_exhausted_rejects():
    d = _ev(
        policy=_pol(approval_mode="auto", max_remote_callouts_per_run=1,
                    require_user_approval_before_first_remote_callout=False),
        target=_target(provider_kind="anthropic", gateway_route_id="anthropic.x"),
        route_kind="remote", remote_callouts_done=1,
    )
    assert d.rejected and d.reason_code == "remote_callout_budget_exhausted"


def test_unknown_route_kind_rejects_provider_unavailable():
    d = _ev(route_kind=None)
    assert d.rejected and d.reason_code == "provider_unavailable"


# --- approval-required paths ----------------------------------------------

def test_ask_user_requires_approval():
    d = _ev(policy=_pol(approval_mode="ask_user"))
    assert d.needs_approval


def test_moderator_mode_requires_approval():
    d = _ev(policy=_pol(approval_mode="moderator"))
    assert d.needs_approval


def test_first_remote_callout_requires_approval_even_in_auto():
    d = _ev(
        policy=_pol(approval_mode="auto",
                    require_user_approval_before_first_remote_callout=True),
        target=_target(provider_kind="anthropic", gateway_route_id="anthropic.x"),
        route_kind="remote", remote_callouts_done=0,
    )
    assert d.needs_approval


# --- admit paths -----------------------------------------------------------

def test_auto_local_admits():
    assert _ev().admitted


def test_any_member_mode_admits_member_request():
    d = _ev(policy=_pol(requester_mode="any_member"), requester_type="member")
    assert d.admitted


def test_subsequent_remote_callout_auto_admits_when_first_approval_disabled():
    d = _ev(
        policy=_pol(approval_mode="auto", max_remote_callouts_per_run=3,
                    require_user_approval_before_first_remote_callout=False),
        target=_target(provider_kind="openai", gateway_route_id="openai.x"),
        route_kind="remote", remote_callouts_done=1,
    )
    assert d.admitted
