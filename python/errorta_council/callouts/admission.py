"""F037 callout admission — a pure, deterministic, testable evaluator.

No I/O, no provider calls. Returns one of: admit, reject(reason_code),
approval_required. The scheduler turns the decision into events and (if
admitted) a single gateway call. Mirrors the order in the spec's
"Admission and approval".
"""
from __future__ import annotations

from dataclasses import dataclass

from errorta_council.schema import EscalationPolicy, EscalationRosterEntry

# requester_modes that permit a non-user (member/topology/steward) request.
# user-initiated callouts are always permitted when the policy is enabled.
_MEMBER_REQUEST_MODES = frozenset({
    "any_member", "member_allowlist", "role_allowlist",
    "quorum", "topology", "steward",
})


@dataclass(frozen=True)
class CalloutDecision:
    outcome: str           # "admit" | "reject" | "approval_required"
    reason_code: str | None = None

    @property
    def admitted(self) -> bool:
        return self.outcome == "admit"

    @property
    def needs_approval(self) -> bool:
        return self.outcome == "approval_required"

    @property
    def rejected(self) -> bool:
        return self.outcome == "reject"


def evaluate_callout(
    *,
    policy: EscalationPolicy,
    target: EscalationRosterEntry | None,
    requester_type: str,
    callouts_done: int,
    remote_callouts_done: int,
    route_kind: str | None,
    run_terminal: bool,
) -> CalloutDecision:
    if not policy.enabled:
        return CalloutDecision("reject", "escalation_disabled")
    if run_terminal:
        return CalloutDecision("reject", "run_terminal")
    if target is None:
        return CalloutDecision("reject", "unknown_callout_target")

    # Requester gate. Manual user callouts always allowed when enabled.
    if requester_type != "user":
        if policy.requester_mode not in _MEMBER_REQUEST_MODES:
            return CalloutDecision("reject", "requester_not_allowed")

    # Hard safety override: keep the roster configured but block all spend.
    if policy.approval_mode == "disabled":
        return CalloutDecision("reject", "approval_disabled")

    # Caps.
    if callouts_done >= policy.max_callouts_per_run:
        return CalloutDecision("reject", "callout_budget_exhausted")

    is_remote = route_kind == "remote"
    if route_kind is None:
        return CalloutDecision("reject", "provider_unavailable")
    if is_remote and remote_callouts_done >= policy.max_remote_callouts_per_run:
        return CalloutDecision("reject", "remote_callout_budget_exhausted")

    # Approval.
    if policy.approval_mode in {"ask_user", "moderator"}:
        return CalloutDecision("approval_required")
    # approval_mode == "auto": still require approval for the first remote
    # callout when the policy says so.
    if (
        is_remote
        and remote_callouts_done == 0
        and policy.require_user_approval_before_first_remote_callout
    ):
        return CalloutDecision("approval_required")
    return CalloutDecision("admit")
