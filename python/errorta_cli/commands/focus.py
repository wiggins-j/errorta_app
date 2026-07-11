"""``north-star`` + ``focus`` — read + edit the project's steering inputs
(F147 §8.3; the READ+EDIT parts — mid-run steering delivery is S6).

Grounded against ``routes/coding.py`` (line refs inline, verified this session):

North Star:
* ``north-star [show]``  → ``GET  /coding/projects/{id}``                (coding.py:548)
* ``north-star set``     → ``PUT  /coding/projects/{id}/north-star``     (coding.py:3660)
* ``north-star proposal``→ ``GET  .../north-star-proposal``             (coding.py:4063)
* ``north-star accept``  → ``POST .../north-star-proposal/accept``      (coding.py:4073)

Current Focus (F137):
* ``focus [list]``       → ``GET  /coding/projects/{id}/focus?status=`` (coding.py:4152)
* ``focus add``          → ``POST /coding/projects/{id}/focus``         (coding.py:4166)
* ``focus edit <id>``    → ``PUT  .../focus/{focus_id}``               (coding.py:4195)
* ``focus reorder <ids>``→ ``PUT  .../focus/reorder``                  (coding.py:4183)
* ``focus accept <id>``  → ``POST .../focus/{focus_id}/accept``        (coding.py:4219)
* ``focus work-request`` → ``PUT  /coding/projects/{id}/work-request`` (coding.py:4106)

**409-while-live is a real, documented outcome.** ``north-star accept`` and
``focus accept`` are human-accept GATES the engine refuses while a run thread is
live (coding.py:4083 / 4228). The client maps that 409 to ``LockBusy`` (exit 3),
and this command renders a clear "a run is live — cancel it or wait" message
rather than a stack trace.

Edits go through ``_mutate.guard_sole_owner`` + the confirm/``--yes`` gate
(invariants #5/#7); reads (``show`` / ``list`` / ``proposal``) don't guard.
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..errors import LockBusy
from ..registry import Command, Param, register, render_json
from ..render import is_no_project, muted, no_project, render
from ..render import project as _rp
from ..session import Context
from . import _base, _mutate


def _text_arg(args: dict[str, Any]) -> str:
    """The single positional text argument (a title / directive).

    The S1 registry maps every non-flag param to a positional slot in order, so
    a multi-word UNQUOTED title would spill into the value-option params. A title
    / directive is therefore taken as ONE argument: ``focus add "add sprites"``
    (argv quoting; the REPL's whitespace parser has the same single-token
    convention until quoted-arg parsing lands).
    """
    return str(args.get("a") or "").strip()


def _run_live_hint(exc: LockBusy) -> LockBusy:
    """Re-message a 409-while-live so the refusal is obvious (not a raw lock error)."""
    return LockBusy(
        f"{exc.message} — this accept is a gate that can't run mid-flight; "
        "cancel the run (errorta cancel) or wait for it to stop, then retry",
        code=exc.code,
    )


# --------------------------------------------------------------------------- #
# north-star.
# --------------------------------------------------------------------------- #

def _north_star_call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    pid = ctx.project_id
    sub = str(args.get("sub") or "show").lower()

    if sub in ("show", ""):
        return {"_kind": "show", "project": (
            client.get_json(f"/coding/projects/{pid}") or {}).get("project")}

    if sub == "proposal":
        return {"_kind": "proposal",
                "proposal": (client.get_json(
                    f"/coding/projects/{pid}/north-star-proposal") or {}).get("proposal")}

    if sub == "set":
        ns = args.get("north-star")
        dod = args.get("dod")
        if ns is None and dod is None:
            return _base.usage("north-star set --north-star \"...\" [--dod \"...\"]")
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, "update the North Star",
                               note="edits the project's North Star / Definition of Done",
                               interactive_prompt=False):
            return {"_kind": "aborted"}
        body: dict[str, Any] = {}
        if ns is not None:
            body["north_star"] = str(ns)
        if dod is not None:
            body["definition_of_done"] = str(dod)
        return {"_kind": "set",
                "project": (client.put_json(
                    f"/coding/projects/{pid}/north-star", json=body) or {}).get("project")}

    if sub == "accept":
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, "accept the North Star proposal",
                               note="promotes the inferred North Star to authoritative",
                               interactive_prompt=False):
            return {"_kind": "aborted"}
        try:
            result = client.post_json(
                f"/coding/projects/{pid}/north-star-proposal/accept", json={})
        except LockBusy as exc:
            raise _run_live_hint(exc) from exc
        return {"_kind": "accepted", "result": result}

    return _base.usage("north-star [show] | set --north-star ... [--dod ...] "
                       "| proposal | accept")


def _north_star_render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    if is_no_project(payload):
        return no_project()
    usage = payload.get("_usage") if isinstance(payload, dict) else None
    if usage:
        return render(muted(f"usage: {usage}"))
    kind = (payload or {}).get("_kind")
    if kind == "aborted":
        return render(muted("aborted — North Star unchanged."))
    if kind == "proposal":
        return _rp.render_proposal({"proposal": payload.get("proposal")}, verbosity)
    if kind == "accepted":
        return render("North Star proposal accepted — it's now authoritative.")
    if kind in ("show", "set"):
        return _rp.render_north_star({"project": payload.get("project")}, verbosity)
    return render(muted("north-star: nothing to show"))


# --------------------------------------------------------------------------- #
# focus (Current Focus goals).
# --------------------------------------------------------------------------- #

_FOCUS_STATUSES = ("active", "completed", "archived", "all")


def _focus_call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    pid = ctx.project_id
    sub = str(args.get("sub") or "list").lower()

    if sub in ("list", ""):
        status = str(args.get("status") or "active").lower()
        if status not in _FOCUS_STATUSES:
            return _base.usage(f"focus list --status {'|'.join(_FOCUS_STATUSES)}")
        return {"_kind": "list", "focuses": (client.get_json(
            f"/coding/projects/{pid}/focus", params={"status": status}) or {}).get("focuses")}

    if sub == "add":
        title = _text_arg(args)
        if not title:
            return _base.usage("focus add <title> [--body \"...\"]")
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, "add a Current Focus",
                               note="adds a Current Focus goal", interactive_prompt=False):
            return {"_kind": "aborted"}
        body = {"title": title, "body": str(args.get("body") or "")}
        return {"_kind": "one", "one": client.post_json(
            f"/coding/projects/{pid}/focus", json=body)}

    if sub == "edit":
        focus_id = str(args.get("a") or "").strip()
        if not focus_id:
            return _base.usage("focus edit <focus_id> [--title ...] [--body ...] [--status ...]")
        patch: dict[str, Any] = {}
        for arg_key, field in (("title", "title"), ("body", "body"), ("status", "status")):
            if args.get(arg_key) is not None:
                patch[field] = str(args[arg_key])
        if not patch:
            return _base.usage("focus edit <focus_id> needs --title / --body / --status")
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, "edit a Current Focus",
                               note="edits a Current Focus goal", interactive_prompt=False):
            return {"_kind": "aborted"}
        return {"_kind": "one", "one": client.put_json(
            f"/coding/projects/{pid}/focus/{focus_id}", json=patch)}

    if sub == "reorder":
        raw = str(args.get("a") or "")
        ordered = [x.strip() for x in raw.split(",") if x.strip()]
        if not ordered:
            return _base.usage("focus reorder <id,id,...>")
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, "reorder Current Focus",
                               note="reorders the active Current Focus goals",
                               interactive_prompt=False):
            return {"_kind": "aborted"}
        return {"_kind": "list", "focuses": (client.put_json(
            f"/coding/projects/{pid}/focus/reorder",
            json={"ordered_ids": ordered}) or {}).get("focuses")}

    if sub == "accept":
        focus_id = str(args.get("a") or "").strip()
        if not focus_id:
            return _base.usage("focus accept <focus_id>")
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, "accept (archive) a completed Current Focus",
                               note="archives a completed Current Focus goal",
                               interactive_prompt=False):
            return {"_kind": "aborted"}
        try:
            result = client.post_json(
                f"/coding/projects/{pid}/focus/{focus_id}/accept", json={})
        except LockBusy as exc:
            raise _run_live_hint(exc) from exc
        return {"_kind": "one", "one": result}

    if sub == "work-request":
        text = _text_arg(args)
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, "set the current-focus work request",
                               note="sets the project's Current Focus directive",
                               interactive_prompt=False):
            return {"_kind": "aborted"}
        return {"_kind": "work_request", "project": client.put_json(
            f"/coding/projects/{pid}/work-request", json={"work_request": text})}

    return _base.usage("focus [list] [--status ...] | add <title> [--body ...] "
                       "| edit <id> [--title/--body/--status ...] | reorder <id,id> "
                       "| accept <id> | work-request <text>")


def _focus_render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    if is_no_project(payload):
        return no_project()
    usage = payload.get("_usage") if isinstance(payload, dict) else None
    if usage:
        return render(muted(f"usage: {usage}"))
    kind = (payload or {}).get("_kind")
    if kind == "aborted":
        return render(muted("aborted — Current Focus unchanged."))
    if kind == "list":
        return _rp.render_focus_list({"focuses": payload.get("focuses")}, verbosity)
    if kind == "one":
        return _rp.render_focus_one(payload.get("one"), verbosity)
    if kind == "work_request":
        return render("current-focus work request set.")
    return render(muted("focus: nothing to show"))


# --------------------------------------------------------------------------- #
# Registration.
# --------------------------------------------------------------------------- #

_YES = Param("yes", "Skip the confirmation prompt (required non-interactively).",
             is_flag=True)

register(Command(
    name="north-star",
    help="Show / set the North Star + Definition of Done (or accept a proposal).",
    call=_north_star_call,
    render=_north_star_render,
    params=(
        Param("sub", "show | set | proposal | accept", default="show"),
        Param("north-star", "set: the new North Star.", is_flag=False),
        Param("dod", "set: the new Definition of Done.", is_flag=False),
        _YES,
    ),
    mutating=True,
))

register(Command(
    name="focus",
    help="List / add / edit / reorder / accept Current Focus goals.",
    call=_focus_call,
    render=_focus_render,
    params=(
        Param("sub", "list | add | edit | reorder | accept | work-request",
              default="list"),
        Param("a", "focus id / title / ids / text (single subcommand arg).",
              default=None),
        Param("status", "list: active|completed|archived|all; edit: new status.",
              is_flag=False),
        Param("title", "edit: new title.", is_flag=False),
        Param("body", "add/edit: goal body.", is_flag=False),
        _YES,
    ),
    mutating=True,
))
