"""``pm`` — read the PM surfaces (S2) + steer via the PM (S6).

Grounded against ``routes/coding.py`` (verified this session):

Reads (S2, unchanged):
* ``pm`` / ``pm chat``   → ``GET  /coding/projects/{id}/pm-chat``    (coding.py:1736)
* ``pm changes``         → ``GET  /coding/projects/{id}/pm-changes``  (coding.py:1930)

Steering (S6):
* ``pm "<question>"`` / ``pm ask "<question>"``
      → ``POST /coding/projects/{id}/pm-ask``   (coding.py:1650) body ``{"message": q}``
        — a synchronous PM chat turn; coexists with a live run (a bounded model call).
* ``pm control ["<directive>"] [--actions JSON]``
      → ``POST /coding/projects/{id}/pm-control`` (coding.py:1999) body
        ``{"actions": [...]}`` OR ``{"directive": "..."}``. Structured actions use the
        REAL catalog (``control_actions.KNOWN_ACTION_TYPES``): ``assign_models``
        ({role_routes}), ``set_autonomy`` ({knobs}), ``set_governance`` ({fields}),
        ``create_task`` ({title,detail,role}), ``start_run``. Response
        ``{applied, refusals, run_started}`` — each per-action refusal is grounded.
* ``pm accept <change_id>`` / ``pm decline <change_id>``
      → ``POST .../pm-changes/{change_id}/accept|decline`` (coding.py:1943/1956).

All PM steering is allowed mid-run (that is the point); ``guard_sole_owner`` only
refuses a FOREIGN app (invariant #6). Reads don't guard. The question / directive is
a single quoted positional (the S1 tokenizer limitation); structured action bodies
arrive as a ``--actions`` JSON array, never smuggled as loose argv values.
"""
from __future__ import annotations

import json as _json
from typing import Any

from ..client import SidecarClient
from ..errors import CliError
from ..registry import Command, Param, register, render_json
from ..render import is_no_project, muted, no_project, render
from ..render.pm import render_pm
from ..session import Context
from . import _base, _mutate

_READ_SUBS = ("chat", "changes")
_MUTATION_SUBS = ("ask", "control", "accept", "decline")


def _reject_watched_mutation(args: dict[str, Any]) -> None:
    """A mutation sub can't be watched — re-firing it every tick spends budget."""
    if args.get("watch"):
        raise CliError(
            "--watch is for the read views (`pm chat` / `pm changes`); a PM "
            "steering action can't be watched (it would re-fire every tick).",
            code="watch_on_mutation")


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    pid = ctx.project_id
    sub = str(args.get("sub") or "chat").lower()

    # -- reads ---------------------------------------------------------------
    if sub == "changes":
        payload = dict(client.get_json(f"/coding/projects/{pid}/pm-changes") or {})
        payload["_sub"] = "changes"
        return payload
    if sub == "chat":
        payload = dict(client.get_json(f"/coding/projects/{pid}/pm-chat") or {})
        payload["_sub"] = "chat"
        return payload

    # -- steering (mutations) ------------------------------------------------
    if sub == "control":
        _reject_watched_mutation(args)
        return _control(client, ctx, args, pid)
    if sub in ("accept", "decline"):
        _reject_watched_mutation(args)
        change_id = str(args.get("a") or "").strip()
        if not change_id:
            return _base.usage(f"pm {sub} <change_id>")
        _mutate.guard_sole_owner(ctx)
        verb = "accept" if sub == "accept" else "decline"
        if not _mutate.confirm(ctx, args, f"{verb} PM change '{change_id}'",
                               note=("applies the PM change" if sub == "accept"
                                     else "reverts the PM change to the prior config"),
                               interactive_prompt=False):
            return {"_kind": "aborted"}
        return {"_kind": "change", "change": (client.post_json(
            f"/coding/projects/{pid}/pm-changes/{change_id}/{sub}", json={})
            or {}).get("change")}

    # `pm ask "<q>"` OR the bare fallback `pm "<q>"` (any unknown non-empty sub).
    _reject_watched_mutation(args)
    if sub == "ask":
        question = str(args.get("a") or "").strip()
    else:
        question = str(args.get("sub") or "").strip()
    if not question or question in _READ_SUBS:
        return _base.usage('pm "<question>" | pm chat | pm changes | pm control | '
                           "pm accept <id> | pm decline <id>")
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, "ask the PM",
                           note="runs one bounded PM model turn",
                           interactive_prompt=False):
        return {"_kind": "aborted"}
    result = client.post_json(
        f"/coding/projects/{pid}/pm-ask", json={"message": question}) or {}
    result["_kind"] = "ask"
    return result


def _control(client: SidecarClient, ctx: Context, args: dict[str, Any],
             pid: str | None) -> dict[str, Any]:
    raw_actions = args.get("actions")
    directive = str(args.get("a") or "").strip()
    body: dict[str, Any]
    if raw_actions is not None:
        try:
            actions = _json.loads(str(raw_actions))
        except ValueError as exc:
            raise CliError(f"--actions must be a JSON array: {exc}",
                           code="bad_actions_json") from exc
        if not isinstance(actions, list):
            raise CliError("--actions must be a JSON array of action objects",
                           code="bad_actions_json")
        body = {"actions": actions}
    elif directive:
        body = {"directive": directive}
    else:
        return _base.usage('pm control "<directive>"  |  pm control --actions '
                           "'[{\"type\":\"assign_models\",\"role_routes\":"
                           "{\"dev\":\"sonnet\"}}]'")
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, "apply PM control-actions",
                           note="changes team config (each change is reviewable); "
                                "a start_run action spends model budget",
                           interactive_prompt=False):
        return {"_kind": "aborted"}
    result = client.post_json(
        f"/coding/projects/{pid}/pm-control", json=body) or {}
    result["_kind"] = "control"
    return result


# --------------------------------------------------------------------------- #
# Rendering — reads delegate to render_pm; steering kinds render here.
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
    if (payload or {}).get("_sub") in _READ_SUBS:
        return render_pm(payload, verbosity)
    if kind == "aborted":
        return render(muted("aborted — nothing changed."))
    if kind == "ask":
        reply = (payload or {}).get("reply") or {}
        text = reply.get("message") or "(no reply)"
        lines = [render(text)]
        lines += _applied_refusal_lines(payload)
        return "\n".join(lines)
    if kind == "control":
        applied = (payload or {}).get("applied") or []
        lines = [render(f"applied {len(applied)} change(s).")]
        for c in applied:
            if isinstance(c, dict):
                lines.append(render(muted(
                    f"  {c.get('change_id') or c.get('id') or ''}: "
                    f"{c.get('summary') or c.get('kind') or ''}")))
        lines += _applied_refusal_lines(payload, include_applied=False)
        return "\n".join(lines)
    if kind == "change":
        change = (payload or {}).get("change") or {}
        return render(
            f"{change.get('status', 'updated')} PM change "
            f"{change.get('change_id') or change.get('id') or ''}",
            muted(str(change.get("summary") or "")))
    return render_pm(payload, verbosity)


def _applied_refusal_lines(payload: Any, *, include_applied: bool = True) -> list[str]:
    lines: list[str] = []
    p = payload or {}
    if include_applied and (p.get("applied")):
        lines.append(render(muted(f"applied {len(p['applied'])} change(s)")))
    for r in (p.get("refusals") or []):
        if isinstance(r, dict):
            lines.append(render(muted(
                f"refused: {r.get('code') or '?'} — {r.get('reason') or ''}")))
    if p.get("run_started"):
        lines.append(render("a run was started."))
    return lines


register(
    Command(
        name="pm",
        help="PM: read (`pm chat`/`pm changes`) or steer (`pm \"<q>\"` / control / "
             "accept / decline).",
        call=_call,
        render=_render,
        params=(
            Param("sub", "chat | changes | ask | control | accept | decline | "
                  "<question>", default="chat"),
            Param("a", "change_id / question / directive (single sub arg).",
                  default=None),
            Param("actions", "control: a JSON array of control-actions.",
                  is_flag=False),
            Param("watch", "re-render a read view on the poll loop", is_flag=True),
            Param("yes", "Skip the confirmation prompt (required non-interactively).",
                  is_flag=True),
        ),
    )
)
