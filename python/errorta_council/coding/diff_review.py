"""F087-11 — structured diff review + evidence-gated merge-back.

Two pure, deterministic concerns, no I/O and no model involvement:

1. ``parse_unified_diff`` turns a ``git diff`` cumulative blob (what
   ``ApplyWorkspace`` emits) into structured per-file changes — path, change
   type, +/- line counts, and hunks — so the human reviews a readable,
   per-file view instead of one opaque blob.
2. ``evaluate_merge_gate`` computes an overridable, evidence-based gate from
   EXPLICIT inputs (ledger task states, the latest reviewer verdict, the latest
   derived test verdict, and conflicting paths). "Accept" then means "accept
   reviewed, tested, complete work," not "accept whatever is in the worktree."

Inputs to the gate are passed in, never read from the ledger, so this module
stays decoupled from the in-flight F087-08/10 storage and is trivially testable.
Wiring a route that assembles those inputs + enforces the gate on accept is the
integration JOIN, intentionally not done here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

# --- structured diff --------------------------------------------------------


@dataclass(frozen=True)
class Hunk:
    header: str
    lines: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class FileDiff:
    path: str
    old_path: Optional[str]  # set for renames
    change_type: str  # added | modified | deleted | renamed
    added_lines: int
    removed_lines: int
    hunks: List[Hunk] = field(default_factory=list)


def _strip_ab(path: str) -> str:
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _parse_block(lines: List[str]) -> Optional[FileDiff]:
    """Parse one ``diff --git`` block into a FileDiff (None if no usable path)."""
    change_type = "modified"
    old_path: Optional[str] = None
    new_path: Optional[str] = None
    a_path: Optional[str] = None
    b_path: Optional[str] = None
    rename_from: Optional[str] = None
    rename_to: Optional[str] = None
    added = 0
    removed = 0
    hunks: List[Hunk] = []
    cur_header: Optional[str] = None
    cur_body: List[str] = []

    def flush() -> None:
        nonlocal cur_header, cur_body
        if cur_header is not None:
            hunks.append(Hunk(header=cur_header, lines=list(cur_body)))
        cur_header = None
        cur_body = []

    # the first line is the "diff --git a/x b/y" header; capture its b-path
    header0 = lines[0] if lines else ""
    parts = header0.split()
    if len(parts) >= 4:
        a_path = _strip_ab(parts[2])
        b_path = _strip_ab(parts[3])

    for line in lines[1:]:
        if line.startswith("new file mode"):
            change_type = "added"
        elif line.startswith("deleted file mode"):
            change_type = "deleted"
        elif line.startswith("rename from "):
            change_type = "renamed"
            rename_from = line[len("rename from ") :].strip()
        elif line.startswith("rename to "):
            change_type = "renamed"
            rename_to = line[len("rename to ") :].strip()
        elif line.startswith("--- "):
            p = line[4:].strip()
            a_path = None if p == "/dev/null" else _strip_ab(p)
        elif line.startswith("+++ "):
            p = line[4:].strip()
            new_path = None if p == "/dev/null" else _strip_ab(p)
        elif line.startswith("@@"):
            flush()
            cur_header = line
        elif cur_header is not None:
            cur_body.append(line)
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
    flush()

    if change_type == "renamed":
        old_path = rename_from or a_path
        path = rename_to or new_path or b_path
    elif change_type == "deleted":
        path = a_path or b_path
    else:
        path = new_path or b_path or a_path

    if not path:
        return None
    return FileDiff(
        path=path,
        old_path=old_path,
        change_type=change_type,
        added_lines=added,
        removed_lines=removed,
        hunks=hunks,
    )


def parse_unified_diff(diff_text: str) -> List[FileDiff]:
    """Parse ``git diff`` unified output into per-file structured changes.

    Robust to empty / whitespace-only input (returns ``[]``). Header lines
    (``+++``/``---``) are never counted as added/removed body lines.
    """
    if not diff_text or not diff_text.strip():
        return []

    blocks: List[List[str]] = []
    current: List[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)

    out: List[FileDiff] = []
    for block in blocks:
        fd = _parse_block(block)
        if fd is not None:
            out.append(fd)
    return out


# --- evidence-gated merge-back ----------------------------------------------


@dataclass(frozen=True)
class MergeBlocker:
    code: str
    detail: str


@dataclass(frozen=True)
class MergeGate:
    allowed: bool
    blockers: List[MergeBlocker] = field(default_factory=list)
    allow_override: bool = True


_TERMINAL_STATES = {"done", "dropped"}


def evaluate_merge_gate(
    *,
    tasks: List[dict[str, Any]],
    reviewed_approved: Optional[bool],
    tests_passed: Optional[bool],
    conflicts: List[str],
    definition_of_done_met: bool,
    preview_ok: bool = True,
    pm_reviewed_approved: Optional[bool] = None,
    require_pm_review: bool = False,
    tests_required: bool = True,
    assembled_run_unverified: bool = False,
) -> MergeGate:
    """Compute the merge-back gate from explicit evidence.

    Each condition is an independent blocker. ``allowed`` is true only when
    there are no blockers; ``allow_override`` is ALWAYS true (a human can force
    a partial merge, but the UI must make it a deliberate, separate action).

    F087-15 M1: ``preview_ok=False`` (the worktree diff could not be produced —
    missing/corrupt worktree) is itself a blocker, never silently "no conflicts".

    F100 PR-B: in ``strict`` governance mode the merge gate is a DUAL review —
    the reviewer role AND the PM must both approve the PR head. Callers signal
    this with ``require_pm_review=True`` and pass the PM's verdict in
    ``pm_reviewed_approved`` (None = no PM verdict yet, False = PM rejected,
    True = PM approved). When ``require_pm_review`` is False (off/light mode)
    the PM review is ignored entirely — today's single-reviewer behavior.
    """
    blockers: List[MergeBlocker] = []

    if not preview_ok:
        blockers.append(MergeBlocker(
            code="preview_unavailable",
            detail="could not read the worktree diff (missing or corrupt worktree)"))

    open_tasks = [
        t
        for t in tasks
        if t.get("state") not in _TERMINAL_STATES and t.get("state") != "blocked"
    ]
    if open_tasks:
        blockers.append(
            MergeBlocker(
                code="open_tasks",
                detail=f"{len(open_tasks)} task(s) not done or dropped",
            )
        )

    blocked = [t for t in tasks if t.get("state") == "blocked"]
    if blocked:
        blockers.append(
            MergeBlocker(
                code="open_blockers",
                detail=f"{len(blocked)} task(s) blocked",
            )
        )

    if reviewed_approved is None:
        blockers.append(
            MergeBlocker(
                code="unreviewed_changes",
                detail="no reviewer verdict yet",
            )
        )
    elif reviewed_approved is False:
        blockers.append(
            MergeBlocker(
                code="review_rejected",
                detail="latest reviewer verdict rejected the changes",
            )
        )

    # F100 PR-B: strict mode requires the PM's review too (reviewer AND PM).
    if require_pm_review:
        if pm_reviewed_approved is None:
            blockers.append(
                MergeBlocker(
                    code="pm_unreviewed_changes",
                    detail="no PM verdict yet (strict mode requires reviewer + PM)",
                )
            )
        elif pm_reviewed_approved is False:
            blockers.append(
                MergeBlocker(
                    code="pm_review_rejected",
                    detail="latest PM verdict rejected the changes",
                )
            )

    if tests_passed is None:
        # F146 Slice D: a project with NO registered test commands AND NO runnable
        # runtime has nothing a delivery test/launch could run, so the tests gate
        # is vacuously satisfied (mirrors the per-PR `_set_mergeable_if_ready`
        # rule) — don't block it forever on a verdict it can never produce. When
        # tests OR a runtime DO exist (``tests_required=True``, the default), a
        # missing verdict still blocks as before.
        if tests_required:
            blockers.append(
                MergeBlocker(code="tests_missing", detail="no test verdict yet")
            )
    elif tests_passed is False:
        blockers.append(
            MergeBlocker(code="tests_failing", detail="latest test run failed")
        )

    if conflicts:
        blockers.append(
            MergeBlocker(
                code="file_conflicts",
                detail="conflicting paths: " + ", ".join(conflicts),
            )
        )

    if not definition_of_done_met:
        blockers.append(
            MergeBlocker(
                code="definition_of_done",
                detail="definition of done not met",
            )
        )

    # Spec 05 Phase A: close the vacuous-pass loophole. A web/app deliverable with
    # NO runnable runtime profile and NO registered assembled/acceptance test
    # command has nothing that verifies it actually runs — the old gate let it
    # through vacuously (tests_passed=None + tests_required=False). When the
    # ``assembled_run_required`` policy is on AND the deliverable looks like a
    # web/app (computed in gather_merge_evidence), refuse instead of passing
    # vacuously. Operator-overridable like every other blocker (allow_override).
    if assembled_run_unverified:
        blockers.append(
            MergeBlocker(
                code="assembled_run_unverified",
                detail=("web/app deliverable has no runnable runtime and no "
                        "assembled acceptance test — nothing verifies the app "
                        "actually runs"),
            )
        )

    return MergeGate(
        allowed=not blockers,
        blockers=blockers,
        allow_override=True,
    )


__all__ = [
    "Hunk",
    "FileDiff",
    "parse_unified_diff",
    "MergeBlocker",
    "MergeGate",
    "evaluate_merge_gate",
]
