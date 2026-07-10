"""Derived project-list status for the Coding Team all-projects view."""
from __future__ import annotations

from typing import Literal

ProjectListStatus = Literal["running", "needs attention"] | str

_NEEDS_ATTENTION_REASONS = {
    "member_unhealthy": "member_unhealthy",
    "worker_unproductive": "worker_unproductive",
    "auth_failed": "auth_failed",
    "binary_missing": "auth_failed",
    "rate_limited": "auth_failed",
    "preflight_blocked": "auth_failed",
    "member_health_preflight_failed": "auth_failed",
    "awaiting_governance_approval": "approval_required",
    "approval_required": "approval_required",
    "review_required": "approval_required",
    "governance_blocked": "approval_required",
    "governance_review_not_converging": "approval_required",
    "blocked": "conflict_or_blocker",
    "blocked_on_problem": "conflict_or_blocker",
    "hard_blocker": "conflict_or_blocker",
    "checkpoint": "conflict_or_blocker",
    "interrupted": "conflict_or_blocker",
    "merge_conflict": "conflict_or_blocker",
    "no_actionable_work": "conflict_or_blocker",
    "no_governance_pm": "conflict_or_blocker",
    "no_governance_reviewer": "conflict_or_blocker",
    "no_progress": "conflict_or_blocker",
    "completion_blocked": "conflict_or_blocker",
    "test_failure_blocked": "conflict_or_blocker",
    "budget_exhausted": "budget_exhausted",
}

_NON_ATTENTION_REASONS = {
    "",
    "cancelled",
    "completed",
    "definition_of_done",
    "done",
    "user_cancelled",
}


def _normalize(value: str | None) -> str:
    return str(value or "").strip().lower()


def derive_project_list_status(
    *,
    lifecycle_status: str,
    run_status: str | None,
    running: bool,
    stop_reason: str | None,
    has_blocking_attention: bool,
) -> tuple[ProjectListStatus, str]:
    """Return the all-projects badge label and stable reason.

    The persisted project lifecycle status is intentionally a fallback. Live
    run state and unresolved blocking Problems are more useful in the list.
    """
    if running:
        return "running", "live_run"

    if has_blocking_attention:
        return "needs attention", "blocking_attention"

    normalized_run_status = _normalize(run_status)
    normalized_reason = _normalize(stop_reason)

    if normalized_run_status == "failed":
        return "needs attention", "conflict_or_blocker"
    if normalized_run_status == "interrupted":
        return "needs attention", "conflict_or_blocker"

    if normalized_reason in _NEEDS_ATTENTION_REASONS:
        return "needs attention", _NEEDS_ATTENTION_REASONS[normalized_reason]

    if normalized_reason in _NON_ATTENTION_REASONS:
        lifecycle = _normalize(lifecycle_status) or "active"
        return lifecycle, "lifecycle"

    lifecycle = _normalize(lifecycle_status) or "active"
    return lifecycle, "lifecycle"
