"""``task`` — add / edit a backlog task (works mid-run).

Grounded against ``routes/coding.py`` (verified this session):

* ``task new <title> [--role R] [--detail ...] [--depends-on id,id]``
    → ``POST /coding/projects/{id}/tasks`` (coding.py:1360)
      body ``_NewTask{title, role, detail, depends_on}`` (coding.py:214)
* ``task set <task_id> [--state ...] [--title ...] [--detail ...] [--role ...]``
    → ``PATCH /coding/projects/{id}/tasks/{task_id}`` (coding.py:1374; free-form patch dict)

Adding / editing a task is allowed while a run is live (the PM works the updated
backlog next tick) — ``guard_sole_owner`` only refuses a FOREIGN app, never the
CLI's own run (invariant #6). ``role`` is required by ``_NewTask``; default ``dev``.

The read view of the backlog is ``tasks`` / ``board`` (S2) — kept as-is.
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, Param, register, render_json
from ..render import is_no_project, muted, no_project, render
from ..session import Context
from . import _base, _mutate

_ROLES = ("pm", "dev", "reviewer", "tester")


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    pid = ctx.project_id
    sub = str(args.get("sub") or "").lower()

    if sub == "new":
        title = str(args.get("a") or "").strip()
        if not title:
            return _base.usage('task new "<title>" [--role dev] [--detail ...] '
                               "[--depends-on id,id]")
        role = str(args.get("role") or "dev").lower()
        if role not in _ROLES:
            return _base.usage(f"task new: --role must be one of {'|'.join(_ROLES)}")
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, "add a task",
                               note="adds a task to the backlog",
                               interactive_prompt=False):
            return {"_kind": "aborted"}
        body: dict[str, Any] = {"title": title, "role": role,
                                "detail": str(args.get("detail") or "")}
        depends = str(args.get("depends-on") or "")
        dep_ids = [d.strip() for d in depends.split(",") if d.strip()]
        if dep_ids:
            body["depends_on"] = dep_ids
        return {"_kind": "task", "task": (client.post_json(
            f"/coding/projects/{pid}/tasks", json=body) or {}).get("task")}

    if sub == "set":
        task_id = str(args.get("a") or "").strip()
        if not task_id:
            return _base.usage("task set <task_id> [--state ...] [--title ...] "
                               "[--detail ...] [--role ...]")
        patch: dict[str, Any] = {}
        for opt, field in (("state", "state"), ("title", "title"),
                           ("detail", "detail"), ("role", "role")):
            if args.get(opt) is not None:
                patch[field] = str(args[opt])
        if not patch:
            return _base.usage("task set <task_id> needs --state / --title / "
                               "--detail / --role")
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, f"update task '{task_id}'",
                               note="edits a backlog task",
                               interactive_prompt=False):
            return {"_kind": "aborted"}
        return {"_kind": "task", "task": (client.patch_json(
            f"/coding/projects/{pid}/tasks/{task_id}", json=patch) or {}).get("task")}

    return _base.usage("task new <title> [...] | task set <id> [...]")


def _render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    if is_no_project(payload):
        return no_project()
    usage = payload.get("_usage") if isinstance(payload, dict) else None
    if usage:
        return render(muted(f"usage: {usage}"))
    if (payload or {}).get("_kind") == "aborted":
        return render(muted("aborted — backlog unchanged."))
    task = (payload or {}).get("task") or {}
    if not task:
        return render(muted("task: nothing to show"))
    tid = task.get("task_id") or task.get("id") or ""
    return render(
        f"task {tid}: {task.get('title', '')}",
        muted(f"role={task.get('role', '?')}  state={task.get('state', '?')}"),
    )


register(Command(
    name="task",
    help="Add (new) or edit (set) a backlog task — works mid-run.",
    call=_call,
    render=_render,
    params=(
        Param("sub", "new | set", default=""),
        Param("a", "title (new) or task_id (set).", default=None),
        Param("role", "task role (new/set): pm|dev|reviewer|tester.", is_flag=False),
        Param("detail", "task detail body.", is_flag=False),
        Param("state", "set: new task state (todo|doing|blocked|done|dropped).",
              is_flag=False),
        Param("title", "set: new title.", is_flag=False),
        Param("depends-on", "new: comma-separated task ids this depends on.",
              is_flag=False),
        Param("yes", "Skip the confirmation prompt (required non-interactively).",
              is_flag=True),
    ),
    mutating=True,
))
