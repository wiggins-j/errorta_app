"""SPEC-45/46 — the shared machine-readable drop/refuse reason vocabulary.

Every site that drops or refuses a task writes one of these codes into the
decision ``extra`` blob (and mirrors it to ``Task.reason_summary``) so the CLI
can render *why* a task left the backlog instead of a hard-coded prose string.
"""
from __future__ import annotations

from typing import Any

MISSING_CAPABILITY = "missing_capability"   # no role/gate can produce the evidence
OVER_SCOPED = "over_scoped"                  # PM pruned obsolete / over-planned scope
DEPENDENCY_UNMET = "dependency_unmet"        # a prerequisite is not satisfied
PM_PRUNED = "pm_pruned"                       # PM explicitly cancelled the task id
OTHER = "other"

ALL = frozenset({
    MISSING_CAPABILITY, OVER_SCOPED, DEPENDENCY_UNMET, PM_PRUNED, OTHER,
})


def reason_blob(code: str, detail: str = "",
                capability: str | None = None) -> dict[str, Any]:
    """The structured reason recorded on a drop/refuse decision's ``extra``."""
    return {
        "reason_code": code if code in ALL else OTHER,
        "reason_detail": str(detail or ""),
        "capability": capability,
    }
