"""F087-13 WS-1 — assemble the merge-back evidence gate from ledger + worktree.

The F087-11 ``evaluate_merge_gate`` is a pure function over explicit inputs; this
module is the (deferred) integration JOIN that reads those inputs from the
ledger and the isolated worktree so the accept route can enforce the gate. The
gate is the LAST trust boundary before the user's real files — "Accept" must mean
"accept reviewed, tested, complete work," not "accept whatever is in the
worktree."

Pure-ish glue: reads the ledger + the worktree preview, returns plain data. No
model involvement, no mutation.
"""
from __future__ import annotations

from typing import Any, Optional

from .diff_review import (
    FileDiff,
    MergeBlocker,
    MergeGate,
    evaluate_merge_gate,
    parse_unified_diff,
)
from .ledger import LedgerStore

_REVIEW_CHOICES = {"review_approved": True, "review_rejected": False}
# F100 PR-B: the PM's PR review is a distinct verdict from the reviewer's, so
# strict mode can require BOTH before merge.
_PM_REVIEW_CHOICES = {"pm_review_approved": True, "pm_review_rejected": False}


def _governance_mode(store: LedgerStore) -> str:
    """The F100 governance mode (off/light/strict), defaulting to ``off`` when
    no governance store / state is available. Fully guarded."""
    try:
        from .governance import GovernanceStore
        return GovernanceStore.for_ledger(store).load_state().mode
    except Exception:
        return "off"


def _corpus_bound(store: LedgerStore) -> bool:
    """Whether the project grounds against a corpus (F104 S5: the conformance
    signal is only meaningful for a corpus-bound project)."""
    try:
        from errorta_project_grounding.corpus_binding import load_binding
        b = load_binding(store)
        return bool(b.corpus_id) and b.mode != "none"
    except Exception:
        return False


def _has_runnable_runtime(store: LedgerStore) -> bool:
    """F146 Slice D: whether the project has a runnable F101-03 runtime profile
    (a profile with a ``start`` argv to launch). Fully guarded — a read error is
    treated as "no runtime" so it can only relax, never spuriously block."""
    try:
        from .runtime import RuntimeProfileStore
        profiles = RuntimeProfileStore.for_ledger(store).list_profiles()
    except Exception:
        return False
    return any(getattr(p, "start", None) for p in profiles)


def _tests_required(store: LedgerStore) -> bool:
    """F146 Slice D: the delivery test/launch gate applies only when there is
    something to run — registered test commands OR a runnable runtime. A
    genuinely test-less, non-runnable project is vacuously satisfied so it is not
    blocked forever by a ``tests_missing`` verdict it can never produce."""
    try:
        has_tests = bool(store.get_test_commands())
    except Exception:
        has_tests = False
    return has_tests or _has_runnable_runtime(store)


def _reviewed_approved_for_head(store: LedgerStore, head: str) -> Optional[bool]:
    """The most recent reviewer verdict made AGAINST ``head`` (F087-15 H1), or
    None if THIS head has never been reviewed. A review of a different (earlier)
    head does not count — the diff changed since, so it is effectively
    unreviewed."""
    if not head:
        return None
    verdict: Optional[bool] = None
    for d in store.list_decisions():
        if d.get("choice") in _REVIEW_CHOICES and d.get("reviewed_head") == head:
            verdict = _REVIEW_CHOICES[d["choice"]]
    return verdict


def _pm_reviewed_approved_for_head(store: LedgerStore, head: str) -> Optional[bool]:
    """The most recent PM PR-review verdict made AGAINST ``head`` (F100 PR-B),
    or None if THIS head has never been PM-reviewed. Mirrors
    ``_reviewed_approved_for_head`` over the PM review choices — a PM review of
    a different (earlier) head does not count."""
    if not head:
        return None
    verdict: Optional[bool] = None
    for d in store.list_decisions():
        if d.get("choice") in _PM_REVIEW_CHOICES and d.get("reviewed_head") == head:
            verdict = _PM_REVIEW_CHOICES[d["choice"]]
    return verdict


def _tests_passed_for_head(store: LedgerStore, head: str) -> Optional[bool]:
    """The most recent GROUNDED test-run verdict AGAINST ``head`` (F087-15 H1),
    or None if THIS head has not been tested."""
    if not head:
        return None
    verdict: Optional[bool] = None
    for r in store.list_test_runs():
        if r.get("head") == head:
            verdict = bool(r.get("passed"))
    return verdict


def _launch_passed_for_head(store: LedgerStore, head: str) -> Optional[bool]:
    """F146 Slice C: the most recent runtime LAUNCH verdict AGAINST ``head``, or
    None if this head was never launched. Fully guarded — a read error yields None
    (no evidence), never a spurious pass."""
    if not head:
        return None
    try:
        from .runtime import RuntimeProfileStore
        recs = RuntimeProfileStore.for_ledger(store).list_runtime_tests()
    except Exception:
        return None
    verdict: Optional[bool] = None
    for r in recs:
        if r.get("kind") == "launch" and r.get("head") == head:
            verdict = bool(r.get("passed"))
    return verdict


def _delivery_tests_passed(store: LedgerStore, head: str) -> Optional[bool]:
    """F146: the tests-gate verdict for ``head``. When the project has registered
    test commands, the grounded test-RUN verdict is authoritative (as always).
    When it has NO test commands but IS runnable — a game/app with no unit suite —
    a fresh clean delivery LAUNCH (Slice C) satisfies the ``tests_required``
    obligation instead, so acceptance #1 (no ``tests_missing`` after ``done``)
    holds for a runnable, test-less project. None when the head carries neither."""
    try:
        has_tests = bool(store.get_test_commands())
    except Exception:
        has_tests = False
    if not has_tests and _has_runnable_runtime(store):
        return _launch_passed_for_head(store, head)
    return _tests_passed_for_head(store, head)


def gather_merge_evidence(store: LedgerStore, workspace: Any) -> dict[str, Any]:
    """Collect the explicit inputs ``evaluate_merge_gate`` needs, BOUND to the
    current worktree head (F087-15 H1) and failing closed on a preview error
    (F087-15 M1)."""
    tasks = [{"taskId": t.task_id, "state": t.state} for t in store.list_tasks()]
    try:
        current_head = workspace.head()
    except Exception:
        current_head = ""
    preview_ok = True
    try:
        preview = workspace.preview()
    except Exception:
        # F087-15 M1: a missing/corrupt worktree is a BLOCKER, not "no conflicts".
        preview = {"diff": "", "conflicts": []}
        preview_ok = False
    conflicts = list(preview.get("conflicts") or [])
    try:
        dod_met = store.get_project().status == "done"
    except Exception:
        dod_met = False
    # F104 S5: spec-conformance signal — did an implementer turn carry corpus
    # evidence? Deterministic; only meaningful for a corpus-bound project.
    corpus_bound = _corpus_bound(store)
    try:
        implementer_grounded = store.any_implementer_grounded()
    except Exception:
        implementer_grounded = False
    # F100 PR-B: in strict governance mode a code PR needs the PM's review too.
    require_pm_review = _governance_mode(store) == "strict"
    return {
        "tasks": tasks,
        "reviewed_approved": _reviewed_approved_for_head(store, current_head),
        "tests_passed": _delivery_tests_passed(store, current_head),
        "conflicts": conflicts,
        "definition_of_done_met": dod_met,
        "preview_ok": preview_ok,
        "current_head": current_head,
        "diff": str(preview.get("diff") or ""),
        "corpus_bound": corpus_bound,
        "implementer_grounded": implementer_grounded,
        "require_pm_review": require_pm_review,
        "pm_reviewed_approved": _pm_reviewed_approved_for_head(store, current_head),
        # F146 Slice D: the tests gate applies only when there's something to run.
        "tests_required": _tests_required(store),
    }


def _hunk_to_dict(h: Any) -> dict[str, Any]:
    return {"header": h.header, "lines": list(h.lines)}


def file_diff_to_dict(fd: FileDiff) -> dict[str, Any]:
    return {
        "path": fd.path,
        "oldPath": fd.old_path,
        "changeType": fd.change_type,
        "addedLines": fd.added_lines,
        "removedLines": fd.removed_lines,
        "hunks": [_hunk_to_dict(h) for h in fd.hunks],
    }


def gate_to_dict(gate: MergeGate) -> dict[str, Any]:
    return {
        "allowed": gate.allowed,
        "allowOverride": gate.allow_override,
        "blockers": [{"code": b.code, "detail": b.detail} for b in gate.blockers],
    }


def _apply_grounding_policy(store: LedgerStore, gate: MergeGate,
                           ev: dict[str, Any]) -> MergeGate:
    """F104 S5: spec-conformance policy. Default ``warn`` surfaces the signal but
    never blocks (mirrors F101 D5). ``required_when_corpus_bound`` blocks a
    corpus-bound project whose implementer turns carried NO corpus evidence;
    ``required`` blocks any project that wasn't implementer-grounded. The signal
    is deterministic (recorded corpus_evidence_count), independent of any model
    judgment."""
    try:
        policy = store.get_grounding_policy()
    except Exception:
        policy = "warn"
    if policy in ("off", "warn"):
        return gate
    if policy == "required_when_corpus_bound" and not ev.get("corpus_bound"):
        return gate
    if ev.get("implementer_grounded"):
        return gate
    blocker = MergeBlocker(
        code="implementer_not_grounded",
        detail=("no implementer turn carried corpus evidence; the code may not "
                "match the bound spec (F104 spec-conformance policy)"))
    return MergeGate(allowed=False, blockers=[*gate.blockers, blocker],
                     allow_override=gate.allow_override)


def merge_review(store: LedgerStore, workspace: Any) -> dict[str, Any]:
    """The full structured merge-back review: per-file diff + evidence gate."""
    ev = gather_merge_evidence(store, workspace)
    gate = evaluate_merge_gate(
        tasks=ev["tasks"],
        reviewed_approved=ev["reviewed_approved"],
        tests_passed=ev["tests_passed"],
        conflicts=ev["conflicts"],
        definition_of_done_met=ev["definition_of_done_met"],
        preview_ok=ev["preview_ok"],
        require_pm_review=ev.get("require_pm_review", False),
        pm_reviewed_approved=ev.get("pm_reviewed_approved"),
        tests_required=ev.get("tests_required", True),
    )
    gate = _apply_grounding_policy(store, gate, ev)  # F104 S5
    file_diffs = [file_diff_to_dict(fd) for fd in parse_unified_diff(ev["diff"])]
    return {
        "file_diffs": file_diffs,
        "gate": gate_to_dict(gate),
        "_gate": gate,
        # F104 S5: surface the deterministic conformance signal in the projection.
        "grounding": {
            "corpus_bound": bool(ev.get("corpus_bound")),
            "implementer_grounded": bool(ev.get("implementer_grounded")),
            "policy": _safe_policy(store),
        },
    }


def _safe_policy(store: LedgerStore) -> str:
    try:
        return store.get_grounding_policy()
    except Exception:
        return "warn"


__all__ = [
    "gather_merge_evidence",
    "merge_review",
    "gate_to_dict",
    "file_diff_to_dict",
    "_pm_reviewed_approved_for_head",
]
