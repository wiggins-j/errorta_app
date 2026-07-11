"""``governance`` — read the governance state (S2) + steer it (S6).

Grounded against ``routes/coding.py`` (verified this session):

Read (S2, unchanged):
* ``governance`` / ``governance show`` → ``GET /coding/projects/{id}/governance``

Steering (S6):
* ``governance settings [--mode ...] [--phase ...] [--human-code-approval ...]
      [--max-review-rounds N] [--block-on-problems true|false] [--monitor JSON]``
      → ``PUT /coding/projects/{id}/governance/settings`` (coding.py:670) body
        ``_GovernanceSettingsBody`` — only the fields the user set are sent.
* ``governance approve <id> [--feedback ...]`` / ``governance reject <id> [--feedback ...]``
      → ``POST .../governance/approvals/{id}/approve|reject`` (coding.py:951/979)
        body ``_GovernanceApprovalBody{feedback, actor}``.
* ``governance artifact accept <id>``
      → ``POST .../governance/artifacts/{id}/accept`` (coding.py:893) body
        ``_GovernanceAcceptBody{confirm}`` — we send ``confirm: true`` (the route
        400s without it; the CLI confirm/``--yes`` gate is the user's deliberate step).
* ``governance artifact export-task <id> --target-path PATH [--title ...]``
      → ``POST .../governance/artifacts/{id}/export-task`` (coding.py:1007) body
        ``_GovernanceExportTaskBody{target_path, title}``.

Settings / approvals are allowed mid-run; ``guard_sole_owner`` only refuses a
FOREIGN app (invariant #6). Reads don't guard.
"""
from __future__ import annotations

import json as _json
from typing import Any

from ..client import SidecarClient
from ..errors import CliError
from ..registry import Command, Param, register, render_json
from ..render import is_no_project, muted, no_project, render
from ..render.governance import render_governance
from ..session import Context
from . import _base, _mutate

_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")


def _reject_watched_mutation(args: dict[str, Any]) -> None:
    if args.get("watch"):
        raise CliError(
            "--watch is for the read view; a governance mutation can't be watched "
            "(it would re-fire every tick).", code="watch_on_mutation")


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    pid = ctx.project_id
    sub = str(args.get("sub") or "show").lower()

    if sub in ("show", ""):
        # A plain read — return the raw route payload untouched (keeps --json clean).
        return client.get_json(f"/coding/projects/{pid}/governance") or {}

    if sub == "settings":
        _reject_watched_mutation(args)
        return _settings(client, ctx, args, pid)

    if sub in ("approve", "reject"):
        _reject_watched_mutation(args)
        approval_id = str(args.get("a") or "").strip()
        if not approval_id:
            return _base.usage(f"governance {sub} <approval_id> [--feedback ...]")
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, f"{sub} approval '{approval_id}'",
                               note=f"records a governance {sub}",
                               interactive_prompt=False):
            return {"_kind": "aborted"}
        body = {"feedback": str(args.get("feedback") or ""), "actor": "user"}
        return {"_kind": "approval", "approval": (client.post_json(
            f"/coding/projects/{pid}/governance/approvals/{approval_id}/{sub}",
            json=body) or {}).get("approval")}

    if sub == "artifact":
        _reject_watched_mutation(args)
        return _artifact(client, ctx, args, pid)

    return _base.usage("governance [show] | settings [...] | approve <id> | "
                       "reject <id> | artifact accept <id> | artifact export-task "
                       "<id> --target-path PATH")


def _settings(client: SidecarClient, ctx: Context, args: dict[str, Any],
              pid: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for opt, field in (("mode", "mode"), ("phase", "phase"),
                       ("human-code-approval", "human_code_approval")):
        if args.get(opt) is not None:
            body[field] = str(args[opt])
    if args.get("max-review-rounds") is not None:
        try:
            body["max_review_rounds"] = int(str(args["max-review-rounds"]))
        except ValueError as exc:
            raise CliError("--max-review-rounds must be an integer",
                           code="bad_arg") from exc
    if args.get("block-on-problems") is not None:
        body["block_on_problems"] = _parse_bool(args["block-on-problems"])
    if args.get("monitor") is not None:
        try:
            body["monitor"] = _json.loads(str(args["monitor"]))
        except ValueError as exc:
            raise CliError("--monitor must be a JSON object", code="bad_arg") from exc
    if not body:
        return _base.usage("governance settings needs at least one of --mode / "
                           "--phase / --human-code-approval / --max-review-rounds / "
                           "--block-on-problems / --monitor")
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, "update governance settings",
                           note="changes the governance policy",
                           interactive_prompt=False):
        return {"_kind": "aborted"}
    result = client.put_json(
        f"/coding/projects/{pid}/governance/settings", json=body) or {}
    result["_kind"] = "settings"
    return result


def _artifact(client: SidecarClient, ctx: Context, args: dict[str, Any],
              pid: str | None) -> dict[str, Any]:
    action = str(args.get("a") or "").lower()
    artifact_id = str(args.get("b") or "").strip()
    if action == "accept":
        if not artifact_id:
            return _base.usage("governance artifact accept <artifact_id>")
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, f"force-accept artifact '{artifact_id}'",
                               note="overrides the AI review gate for this artifact",
                               interactive_prompt=False):
            return {"_kind": "aborted"}
        return {"_kind": "artifact", "artifact": (client.post_json(
            f"/coding/projects/{pid}/governance/artifacts/{artifact_id}/accept",
            json={"confirm": True}) or {}).get("artifact")}
    if action == "export-task":
        if not artifact_id:
            return _base.usage(
                "governance artifact export-task <artifact_id> --target-path PATH")
        target = str(args.get("target-path") or "").strip()
        if not target:
            return _base.usage(
                "governance artifact export-task <id> needs --target-path PATH")
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, f"export artifact '{artifact_id}' as a task",
                               note="creates a DEV task to write the artifact",
                               interactive_prompt=False):
            return {"_kind": "aborted"}
        body: dict[str, Any] = {"target_path": target}
        if args.get("title") is not None:
            body["title"] = str(args["title"])
        return {"_kind": "artifact_task", "task": (client.post_json(
            f"/coding/projects/{pid}/governance/artifacts/{artifact_id}/export-task",
            json=body) or {}).get("task")}
    return _base.usage("governance artifact accept <id> | governance artifact "
                       "export-task <id> --target-path PATH [--title ...]")


def _parse_bool(value: Any) -> bool:
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise CliError("expected true/false", code="bad_arg")


# --------------------------------------------------------------------------- #
# Rendering.
# --------------------------------------------------------------------------- #

def _render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    if is_no_project(payload):
        return no_project()
    usage = payload.get("_usage") if isinstance(payload, dict) else None
    if usage:
        return render(muted(f"usage: {usage}"))
    kind = (payload or {}).get("_kind")
    if kind == "settings":
        return render_governance(payload, verbosity)
    if kind == "aborted":
        return render(muted("aborted — governance unchanged."))
    if kind == "approval":
        a = (payload or {}).get("approval") or {}
        return render(f"approval {a.get('approval_id') or a.get('id') or ''}: "
                      f"{a.get('state', 'updated')}")
    if kind == "artifact":
        a = (payload or {}).get("artifact") or {}
        return render(f"artifact {a.get('artifact_id') or a.get('id') or ''} "
                      f"force-accepted ({a.get('state', '?')}).")
    if kind == "artifact_task":
        t = (payload or {}).get("task") or {}
        return render(f"exported artifact as task {t.get('task_id') or t.get('id') or ''}.")
    # No mutation marker → a plain read payload.
    return render_governance(payload, verbosity)


register(Command(
    name="governance",
    help="Governance: read state, or steer (settings / approve / reject / artifact).",
    call=_call,
    render=_render,
    params=(
        Param("sub", "show | settings | approve | reject | artifact", default="show"),
        Param("a", "approval_id / artifact-action (single sub arg).", default=None),
        Param("b", "artifact_id (governance artifact <action> <id>).", default=None),
        Param("mode", "settings: governance mode.", is_flag=False),
        Param("phase", "settings: governance phase.", is_flag=False),
        Param("human-code-approval", "settings: human code-approval policy.",
              is_flag=False),
        Param("max-review-rounds", "settings: max review rounds (int).", is_flag=False),
        Param("block-on-problems", "settings: true|false.", is_flag=False),
        Param("monitor", "settings: Progress Monitor thresholds (JSON object).",
              is_flag=False),
        Param("feedback", "approve/reject: feedback note.", is_flag=False),
        Param("target-path", "artifact export-task: repo-relative target path.",
              is_flag=False),
        Param("title", "artifact export-task: task title.", is_flag=False),
        Param("watch", "re-render the read view on the poll loop", is_flag=True),
        Param("yes", "Skip the confirmation prompt (required non-interactively).",
              is_flag=True),
    ),
))
