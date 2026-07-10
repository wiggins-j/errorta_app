"""Safe mobile projections for Coding Team projects.

The Coding Team ledger is intentionally rich: it stores prompts, raw model
responses, tool args/results, repo paths, diffs, and test output. Mobile routes
must never return those records directly.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from errorta_council.coding.ledger import LedgerStore, ProjectNotFound, Task, list_projects

_BOARD_COLUMNS = ("todo", "doing", "blocked", "done")
_LIVE_PR_STATUSES = {"open", "changes_requested", "mergeable", "conflict"}


class CodingProjectNotFound(Exception):
    """Raised when a mobile Coding Team projection cannot find a project."""


def project_summaries(*, limit: int = 50) -> list[dict[str, Any]]:
    """Return safe summaries for Coding Team projects."""
    out: list[dict[str, Any]] = []
    for raw in list_projects()[: max(1, min(int(limit), 200))]:
        project_id = str(raw.get("id") or "")
        if not project_id:
            continue
        try:
            out.append(project_summary(LedgerStore(project_id)))
        except Exception:
            continue
    return out


def project_summary(store: LedgerStore) -> dict[str, Any]:
    try:
        project = store.get_project()
    except ProjectNotFound as exc:
        raise CodingProjectNotFound(store.project_id) from exc
    tasks = store.list_tasks()
    run_state = store.get_run_state()
    attention_reasons = _attention_reasons(store, tasks)
    return {
        "project_id": project.id,
        "north_star_summary": _text(project.north_star, 180),
        "status": project.status,
        "run_state": _text(str(run_state.get("status") or "idle"), 40),
        "progress": _progress(tasks),
        "needs_attention": bool(attention_reasons),
        "attention_reasons": attention_reasons,
        "grounding": _grounding_status(store),
        "updated_at": project.updated_at,
    }


def project_detail(store: LedgerStore) -> dict[str, Any]:
    summary = project_summary(store)
    project = store.get_project()
    summary.update(
        {
            "definition_of_done_summary": _text(project.definition_of_done, 220),
            "target": "existing" if project.target == "existing" else "new",
        }
    )
    return summary


def board_projection(store: LedgerStore) -> dict[str, Any]:
    tasks = store.list_tasks()
    prs = store.list_prs()
    tests = store.list_test_runs()
    task_by_id = {task.task_id: task for task in tasks}
    pr_by_task = _prs_by_root_task(prs, task_by_id)
    pr_by_id = {str(pr.get("pr_id") or ""): pr for pr in prs}
    pr_to_root = _pr_to_root_task(prs, task_by_id)
    tests_by_task = _tests_by_root_task(tests, task_by_id, pr_to_root)
    columns = {
        state: [
            _task_projection(
                task,
                pr=_pr_for_task(task, pr_by_task, pr_by_id, task_by_id),
                tests=tests_by_task.get(task.task_id, []),
            )
            for task in tasks
            if task.state == state
        ]
        for state in _BOARD_COLUMNS
    }
    return {"columns": columns}


def pr_projection(store: LedgerStore, *, limit: int = 100) -> dict[str, Any]:
    tasks = {task.task_id: task for task in store.list_tasks()}
    prs = store.list_prs()
    test_by_pr = _tests_by_pr(store.list_test_runs(), prs, tasks)
    items = [
        _pr_item(pr, tests=test_by_pr.get(str(pr.get("pr_id") or ""), []))
        for pr in prs
    ]
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return {"prs": items[: max(1, min(int(limit), 200))]}


def test_run_projection(store: LedgerStore, *, limit: int = 100) -> dict[str, Any]:
    runs = [_test_run_item(run) for run in store.list_test_runs()]
    runs.sort(key=lambda item: item.get("at") or "", reverse=True)
    return {"runs": runs[: max(1, min(int(limit), 200))]}


def activity_projection(store: LedgerStore, *, limit: int = 100) -> dict[str, Any]:
    cap = max(1, min(int(limit), 200))
    activities: list[dict[str, Any]] = []
    for event in store.list_tool_events(limit=cap):
        item = _tool_activity_item(event)
        if item:
            activities.append(item)
    for decision in store.list_decisions()[-cap:]:
        activities.append(_decision_activity_item(decision))
    activities.sort(key=lambda item: item.get("at") or "", reverse=True)
    return {"items": activities[:cap]}


def _task_projection(
    task: Task,
    *,
    pr: dict[str, Any] | None,
    tests: list[dict[str, Any]],
) -> dict[str, Any]:
    badges = [_role_badge(task.role)]
    if pr:
        status = str(pr.get("status") or "open")
        badges.append({"kind": "pr_status", "label": f"PR {status.replace('_', ' ')}"})
        approved = pr.get("reviewer_approved")
        if approved is True:
            badges.append({"kind": "review", "label": "Review approved"})
        elif approved is False:
            badges.append({"kind": "review", "label": "Changes requested"})
        passed = pr.get("tests_passed")
        if passed is True:
            badges.append({"kind": "tests", "label": "Tests passed"})
        elif passed is False:
            badges.append({"kind": "tests", "label": "Tests failed"})
        elif tests:
            latest = tests[-1]
            badges.append(
                {
                    "kind": "tests",
                    "label": "Tests passed" if latest.get("passed") else "Tests failed",
                }
            )
    elif tests:
        latest = tests[-1]
        badges.append(
            {
                "kind": "tests",
                "label": "Tests passed" if latest.get("passed") else "Tests failed",
            }
        )
    return {
        "task_id": task.task_id,
        "title": _text(task.title, 160),
        "role": task.role,
        "state": task.state,
        "assignee_member_id": _text(task.assignee_member_id or "", 80) or None,
        "pr_id": str(pr.get("pr_id")) if pr else task.pr_id,
        "badges": badges,
        "updated_at": task.updated_at,
    }


def _pr_item(pr: dict[str, Any], *, tests: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "pr_id": str(pr.get("pr_id") or ""),
        "task_id": str(pr.get("task_id") or ""),
        "branch_label": _branch_label(str(pr.get("branch") or "")),
        "status": _text(str(pr.get("status") or "open"), 40),
        "review": _review_label(pr.get("reviewer_approved")),
        "tests": _tests_label(pr.get("tests_passed"), tests),
        "conflict_count": len(pr.get("conflicts") or []),
        "created_at": pr.get("created_at"),
        "updated_at": pr.get("updated_at"),
    }


def _test_run_item(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "test_run_id": str(run.get("test_run_id") or ""),
        "task_id": str(run.get("task_id") or ""),
        "passed": bool(run.get("passed")),
        "command_count": len(run.get("command_ids") or []),
        "unknown_count": len(run.get("unknown_ids") or []),
        "sandbox": _text(str(run.get("sandbox") or ""), 80),
        "head_label": _head_label(str(run.get("head") or "")),
        "at": run.get("at"),
    }


def _tool_activity_item(event: dict[str, Any]) -> dict[str, Any] | None:
    tool = _text(str(event.get("tool") or "tool"), 60)
    role = _text(str(event.get("role") or ""), 30)
    status = _text(str(event.get("status") or ""), 40)
    path = _event_path(event)
    summary = " ".join(part for part in (role.upper(), tool, path) if part)
    if not summary:
        return None
    return {
        "activity_id": str(event.get("event_id") or ""),
        "kind": "tool_event",
        "task_id": str(event.get("task_id") or ""),
        "status": status,
        "summary": _text(summary, 180),
        "at": event.get("at"),
    }


def _decision_activity_item(decision: dict[str, Any]) -> dict[str, Any]:
    title = _text(str(decision.get("title") or "Decision"), 100)
    choice = _text(str(decision.get("choice") or ""), 80)
    summary = f"{title}: {choice}" if choice else title
    return {
        "activity_id": str(decision.get("decision_id") or ""),
        "kind": "decision",
        "task_id": _related_task_id(decision),
        "status": "recorded",
        "summary": _text(summary, 180),
        "at": decision.get("at"),
    }


def _prs_by_root_task(
    prs: list[dict[str, Any]],
    task_by_id: dict[str, Task],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for pr in prs:
        root = _root_task_id(str(pr.get("task_id") or ""), task_by_id)
        if root:
            out[root] = pr
    return out


def _tests_by_root_task(
    tests: list[dict[str, Any]],
    task_by_id: dict[str, Task],
    pr_to_root: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for run in tests:
        task_id = str(run.get("task_id") or "")
        task = task_by_id.get(task_id)
        root = pr_to_root.get(task.pr_id or "") if task else ""
        root = root or _root_task_id(task_id, task_by_id)
        if root:
            out.setdefault(root, []).append(run)
    return out


def _tests_by_pr(
    tests: list[dict[str, Any]],
    prs: list[dict[str, Any]],
    task_by_id: dict[str, Task],
) -> dict[str, list[dict[str, Any]]]:
    root_to_pr: dict[str, str] = {}
    for pr in prs:
        root = _root_task_id(str(pr.get("task_id") or ""), task_by_id)
        pr_id = str(pr.get("pr_id") or "")
        if root and pr_id:
            root_to_pr[root] = pr_id
    out: dict[str, list[dict[str, Any]]] = {}
    for run in tests:
        task_id = str(run.get("task_id") or "")
        task = task_by_id.get(task_id)
        pr_id = str(task.pr_id or "") if task else ""
        if not pr_id:
            root = _root_task_id(task_id, task_by_id)
            pr_id = root_to_pr.get(root, "")
        if pr_id:
            out.setdefault(pr_id, []).append(run)
    return out


def _pr_to_root_task(
    prs: list[dict[str, Any]],
    task_by_id: dict[str, Task],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for pr in prs:
        pr_id = str(pr.get("pr_id") or "")
        root = _root_task_id(str(pr.get("task_id") or ""), task_by_id)
        if pr_id and root:
            out[pr_id] = root
    return out


def _pr_for_task(
    task: Task,
    pr_by_task: dict[str, dict[str, Any]],
    pr_by_id: dict[str, dict[str, Any]],
    task_by_id: dict[str, Task],
) -> dict[str, Any] | None:
    if task.pr_id and task.pr_id in pr_by_id:
        return pr_by_id[task.pr_id]
    root = _root_task_id(task.task_id, task_by_id)
    return pr_by_task.get(root)


def _root_task_id(task_id: str, task_by_id: dict[str, Task]) -> str:
    seen: set[str] = set()
    cur = task_id
    while cur and cur in task_by_id and cur not in seen:
        seen.add(cur)
        task = task_by_id[cur]
        if task.role == "dev":
            return task.task_id
        if task.depends_on:
            cur = task.depends_on[0]
            continue
        if task.parent_task_id:
            cur = task.parent_task_id
            continue
        return task.task_id
    return task_id


def _progress(tasks: list[Task]) -> dict[str, int]:
    active = [task for task in tasks if task.state != "dropped"]
    total = len(active)
    done = sum(1 for task in active if task.state == "done")
    doing = sum(1 for task in active if task.state == "doing")
    todo = sum(1 for task in active if task.state == "todo")
    blocked = sum(1 for task in active if task.state == "blocked")
    return {
        "total": total,
        "done": done,
        "doing": doing,
        "todo": todo,
        "blocked": blocked,
        "percent": int(round((done / total) * 100)) if total else 0,
    }


def _attention_reasons(store: LedgerStore, tasks: list[Task]) -> list[str]:
    prs = store.list_prs()
    reasons: list[str] = []
    if any(task.state == "blocked" for task in tasks):
        reasons.append("blocked_task")
    if any(str(pr.get("status") or "") in _LIVE_PR_STATUSES for pr in prs):
        reasons.append("open_pr")
    if any(not run.get("passed") for run in store.list_test_runs()):
        reasons.append("tests_failed")
    return reasons


def _grounding_status(store: LedgerStore) -> dict[str, str]:
    try:
        from errorta_project_grounding.corpus_binding import load_binding

        binding = load_binding(store)
        return {
            "mode": binding.mode,
            "health_state": binding.health_state,
        }
    except Exception:
        return {"mode": "none", "health_state": "missing"}


def _role_badge(role: str) -> dict[str, str]:
    return {"kind": "role", "label": role.upper()}


def _review_label(value: Any) -> str:
    if value is True:
        return "approved"
    if value is False:
        return "changes_requested"
    return "pending"


def _tests_label(value: Any, tests: list[dict[str, Any]]) -> str:
    if value is True:
        return "passed"
    if value is False:
        return "failed"
    if tests:
        return "passed" if tests[-1].get("passed") else "failed"
    return "pending"


def _event_path(event: dict[str, Any]) -> str:
    for section in ("result", "intent"):
        raw = event.get(section)
        if not isinstance(raw, dict):
            continue
        for key in ("path", "file", "target", "cwd"):
            value = raw.get(key)
            if isinstance(value, str):
                safe = _relative_path_label(value)
                if safe:
                    return safe
    return ""


def _relative_path_label(value: str) -> str:
    raw = value.strip().replace("\\", "/")
    if not raw:
        return ""
    if raw.startswith("/") or raw.startswith("~") or re.match(r"^[A-Za-z]:/", raw):
        return ""
    parts = [part for part in PurePosixPath(raw).parts if part not in ("", ".")]
    if not parts or ".." in parts:
        return ""
    return _text("/".join(parts), 120)


def _branch_label(value: str) -> str:
    return _text(value.replace("\\", "/"), 120)


def _head_label(value: str) -> str:
    if not value:
        return ""
    return _text(value, 12)


def _related_task_id(decision: dict[str, Any]) -> str | None:
    ids = decision.get("related_task_ids")
    if isinstance(ids, list) and ids:
        return str(ids[0])
    return None


def _text(value: str | None, max_len: int) -> str:
    # Defense in depth: every projected free-text field funnels through here, so
    # scrub author/model-authored content (a secret token, an absolute home path,
    # an SSH host/IP typed into a North Star / DoD / task title / branch /
    # decision text) BEFORE it can egress to a paired phone. Redaction runs before
    # truncation so a cut can't slice a token mid-redaction.
    raw = str(value or "")
    try:
        from errorta_diagnostics.redact import apply_pipeline
        raw = apply_pipeline(raw)[0]
    except Exception:
        pass
    text = " ".join(raw.split())
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 1)].rstrip() + "..."
