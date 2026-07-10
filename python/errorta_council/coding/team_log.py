"""Coding Team Log — a human-readable narrative of what the team did.

Pure projection over the ledger (project North Star + tasks + turns + the
decision event log) and the optional F100 governance store (spec/plan artifacts,
reviews, approvals), merged chronologically into plain-language entries for the
UI's "Team Log" panel. No model involvement, no mutation, no egress.

Only the narrative-worthy events are rendered; internal bookkeeping (turn
retries, requeues, stale-head revalidations) is intentionally omitted so the log
reads like a story, not a trace.
"""
from __future__ import annotations

from typing import Any

_ARTIFACT_LABEL = {
    "spec": "spec", "implementation_plan": "implementation plan",
    "plan_amendment": "plan amendment", "brainstorm": "brainstorm",
}
_APPROVAL_LABEL = {"spec_approval": "spec", "plan_approval": "implementation plan"}


# Internal re-work tasks the PM auto-creates (not plan-derived work items).
_CORRECTIVE_TASK_PREFIXES = ("revise:", "fix tests:", "resolve conflict:",
                             "review pr:", "test pr:")
# Worker-task title prefixes stripped so a reviewer/tester line names the
# underlying work, not the role's own task wrapper.
_WRAPPER_PREFIXES = ("review pr: ", "test pr: ")


def _short(text: Any, n: int = 90) -> str:
    s = str(text or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _clean_title(text: Any) -> str:
    s = str(text or "").strip()
    low = s.lower()
    for pre in _WRAPPER_PREFIXES:
        if low.startswith(pre):
            s = s[len(pre):].strip()
            break
    return _short(s)


def _is_corrective(title: Any) -> bool:
    low = str(title or "").strip().lower()
    return any(low.startswith(p) for p in _CORRECTIVE_TASK_PREFIXES)


def build_team_log(store: Any) -> list[dict[str, Any]]:
    """Return an ordered list of human-readable team-log entries.

    Each entry: ``{at, role, member, kind, message}`` where ``role`` drives the
    UI badge (PM/DEV/REV/TEST), ``member`` is the member id (e.g. "m-2") or "",
    and ``message`` is the action with NO actor prefix (the UI shows the tag +
    member, so a prefix would double). Sorted oldest-first by timestamp (UI may
    reverse). Fully guarded — a missing governance store or a malformed record
    degrades to fewer entries, never an exception."""
    entries: list[dict[str, Any]] = []

    tasks = store.list_tasks()
    task_by_id = {t.task_id: t for t in tasks}
    title_by_task = {t.task_id: t.title for t in tasks}

    # Attribute a member to (task, role) from the turn transcript so the log can
    # say "Developer (dev-1)" instead of a bare role.
    member_by: dict[tuple[str, str], str] = {}
    for t in store.list_turns():
        tid, role, mid = t.get("task_id"), t.get("role"), t.get("member_id")
        if tid and role and mid:
            member_by[(str(tid), str(role))] = str(mid)

    def member_id(role: str, task_id: str | None = None) -> str:
        """The member that played ``role`` on ``task_id`` (e.g. "m-2"), or "".
        PM/system entries have no member. The UI renders a role TAG (PM/DEV/REV/
        TEST) + this member name, so messages MUST NOT repeat the actor."""
        if role in ("pm", "system") or not task_id:
            return ""
        return member_by.get((str(task_id), role), "")

    def add(at: Any, role: str, member: str, kind: str, message: str) -> None:
        if at:
            entries.append({"at": str(at), "role": role, "member": member,
                            "kind": kind, "message": message})

    def related_ids(decision: dict[str, Any]) -> list[str]:
        return [str(tid) for tid in decision.get("related_task_ids") or [] if tid]

    def display_title(decision: dict[str, Any]) -> str:
        """Prefer the source developer task over review/test wrapper tasks."""
        rel = related_ids(decision)
        for tid in rel:
            task = task_by_id.get(tid)
            if task is not None and task.role == "dev" and not _is_corrective(task.title):
                return _clean_title(task.title)
        for tid in rel:
            if tid in title_by_task:
                return _clean_title(title_by_task[tid])
        return _clean_title(decision.get("title", ""))

    # 0 — North Star.
    try:
        proj = store.get_project()
        add(proj.created_at, "pm", "", "north_star",
            f"reviewed the North Star: {_short(proj.north_star)}")
    except Exception:
        pass

    # 1 — F100 governance: spec/plan artifacts, reviews, approvals.
    try:
        from .governance import GovernanceStore
        gov = GovernanceStore(store.project_id, root=store.dir.parent)
        for a in gov.list_artifacts():
            label = _ARTIFACT_LABEL.get(a.artifact_kind, a.artifact_kind)
            verb = "revised" if int(getattr(a, "version", 1) or 1) > 1 else "created"
            add(a.created_at, "pm", "", "artifact",
                f"{verb} a {label}: {_short(a.title)}")
        for r in gov.list_reviews():
            add(r.created_at, "pm", "", "review",
                f"reviewed an artifact ({len(r.findings)} finding(s))")
        for ap in gov.list_approvals():
            label = _APPROVAL_LABEL.get(ap.kind, ap.kind)
            if ap.state == "approved":
                add(ap.resolved_at or ap.created_at, "pm", "", "approval",
                    f"reviewed and approved the {label}")
            elif ap.state == "rejected":
                add(ap.resolved_at or ap.created_at, "pm", "", "approval",
                    f"requested changes on the {label}")
            else:
                add(ap.created_at, "pm", "", "approval",
                    f"requested approval for the {label}")
    except Exception:
        pass

    # 2 — task creation (substantive plan-derived dev work; skip auto-created
    # corrective re-work like revise:/fix tests:).
    for t in tasks:
        if t.role == "dev" and not _is_corrective(t.title):
            add(t.created_at, "pm", "", "task_created",
                f"created task: {_short(t.title)}")

    # 3 — the decision event log (the dev/review/test/merge flow). Messages carry
    # NO actor prefix (the UI shows the role tag + member name).
    for d in store.list_decisions():
        choice = d.get("choice")
        at = d.get("at")
        rel = related_ids(d)
        tid = rel[0] if rel else None
        title = display_title(d)
        if choice == "context_request":
            add(at, "dev", member_id("dev", tid), "context_request",
                f"requested more context for: {title}")
            add(at, "pm", "", "context_delivered",
                f"delivered context for: {title}")
        elif choice == "pr_opened":
            add(at, "dev", member_id("dev", tid), "pr_opened",
                f"completed the work and opened a PR for: {title}")
        elif choice == "review_approved":
            add(at, "reviewer", member_id("reviewer", tid), "review_approved",
                f"reviewed and approved: {title}")
        elif choice == "pm_review_approved":
            # F100 PR-B: the PM's code-PR review (strict-mode dual gate).
            add(at, "pm", "", "pm_review_approved",
                f"reviewed and approved the PR for: {title}")
        elif choice == "pm_review_rejected":
            add(at, "pm", "", "pm_review_rejected",
                f"requested changes on the PR for: {title}")
        elif choice == "tested_pass":
            add(at, "tester", member_id("tester", tid), "tested_pass",
                f"ran the tests for {title}: passed")
        elif choice == "pr_merged":
            add(at, "pm", "", "pr_merged", f"merged {title} into the project")
        elif choice == "pr_superseded":
            add(at, "pm", "", "pr_superseded",
                f"superseded an earlier PR for: {title}")
        elif choice == "pr_conflict":
            add(at, "pm", "", "pr_conflict",
                f"a merge conflict was detected on: {title}")
        elif choice == "pr_conflict_redispatched":
            add(at, "pm", "", "pr_conflict",
                f"re-dispatched a resolve task for the conflicted: {title}")
        elif choice == "pr_conflict_blocked":
            add(at, "pm", "", "pr_conflict",
                f"blocked the PR for {title} after the conflict-resolve cap")
        elif choice == "blocked":
            add(at, "pm", "", "blocked", f"blocked the task: {title}")
        elif choice == "governance_plan_materialized":
            add(at, "pm", "", "artifact",
                "turned the approved plan into developer tasks")
        elif choice == "run_interrupted":
            add(at, "system", "", "run",
                "the run was interrupted (resumable)")
        elif choice == "human_file_edit":
            # F105 (D2): a human edit is the USER, not a team member. The UI
            # renders a YOU badge for role "user", so the message carries no actor
            # prefix. Path comes from the decision's top-level `path` field
            # (stamped via record_decision extra), not display_title — there is no
            # related dev task. Sha/head/bytes stay out of the prose.
            edited_path = str(d.get("path") or "")
            add(at, "user", "", "human_file_edit", f"edited {edited_path}")
        # everything else (turn retries, requeues, stale revalidations, …) is
        # intentionally omitted to keep the log readable.

    entries.sort(key=lambda e: e.get("at") or "")
    return entries


__all__ = ["build_team_log"]
