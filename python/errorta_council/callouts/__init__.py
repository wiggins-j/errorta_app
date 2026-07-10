"""F037 Council expert callouts.

A callout is a bounded, scheduler-admitted request for a configured expert
(roster) target to answer a specific question during a run. Models/users
*request*; the scheduler *admits and executes*. All provider calls flow
through the existing F034 gateway. Default-off, fail-closed, roster-only.
"""
from errorta_council.callouts.admission import (
    CalloutDecision,
    evaluate_callout,
)
from errorta_council.callouts.ids import new_callout_id
from errorta_council.callouts.policy import resolve_callout_policy
from errorta_council.callouts.queue import CalloutQueue, CalloutRecord

__all__ = [
    "CalloutDecision",
    "CalloutQueue",
    "CalloutRecord",
    "evaluate_callout",
    "new_callout_id",
    "resolve_callout_policy",
]
