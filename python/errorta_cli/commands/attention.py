"""``attention`` — problems/alerts (S2 read) + resolve a signal (S6).

Grounded against ``routes/coding.py`` (verified this session):

* ``attention [--state ...]`` → ``GET /coding/projects/{id}/attention`` (read)
* ``attention resolve <signal_id> --action ACTION [--suggestion-id ID]
      [--correction-file PATH]``
      → ``POST /coding/projects/{id}/attention/{signal_id}/resolve`` (coding.py:751)
        body ``_ResolveSignalBody{action, suggestion_id?, correction_text?}`` (coding.py:242).

Resolving a signal is allowed while a run is live; ``guard_sole_owner`` only refuses
a FOREIGN app (invariant #6). The read path doesn't guard.

**Correction text is never an argv value** (invariant #4): it comes from
``--correction-file PATH`` (a local text file), never a command-line string.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..client import SidecarClient
from ..errors import CliError
from ..registry import Command, Param, register, render_json
from ..render import is_no_project, muted, no_project, render
from ..render.attention import render_attention
from ..session import Context
from . import _base, _mutate


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    pid = ctx.project_id
    sub = str(args.get("sub") or "").lower()

    if sub == "resolve":
        if args.get("watch"):
            raise CliError(
                "--watch is for the read view; resolving a signal can't be watched "
                "(it would re-fire every tick).", code="watch_on_mutation")
        signal_id = str(args.get("a") or "").strip()
        if not signal_id:
            return _base.usage("attention resolve <signal_id> --action ACTION "
                               "[--suggestion-id ID] [--correction-file PATH]")
        action = str(args.get("action") or "").strip()
        if not action:
            return _base.usage("attention resolve <signal_id> needs --action ACTION")
        body: dict[str, Any] = {"action": action}
        if args.get("suggestion-id") is not None:
            body["suggestion_id"] = str(args["suggestion-id"])
        correction_file = args.get("correction-file")
        if correction_file:
            try:
                body["correction_text"] = Path(str(correction_file)).read_text(
                    encoding="utf-8")
            except OSError as exc:
                raise CliError(f"could not read --correction-file: {exc}",
                               code="correction_file_unreadable") from exc
            except UnicodeDecodeError as exc:
                raise CliError("--correction-file must be UTF-8 text",
                               code="correction_file_not_text") from exc
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, f"resolve signal '{signal_id}'",
                               note=f"resolves the attention signal ({action})",
                               interactive_prompt=False):
            return {"_kind": "aborted"}
        result = client.post_json(
            f"/coding/projects/{pid}/attention/{signal_id}/resolve",
            json=body) or {}
        return {"_kind": "resolved", **result}

    # read — return the raw route payload untouched (keeps --json clean).
    params: dict[str, Any] = {}
    if args.get("state"):
        params["state"] = args["state"]
    return client.get_json(
        f"/coding/projects/{pid}/attention", params=params or None) or {}


def _render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    if is_no_project(payload):
        return no_project()
    usage = payload.get("_usage") if isinstance(payload, dict) else None
    if usage:
        return render(muted(f"usage: {usage}"))
    kind = (payload or {}).get("_kind")
    if kind == "aborted":
        return render(muted("aborted — signal unresolved."))
    if kind == "resolved":
        signal = (payload or {}).get("signal") or {}
        created = (payload or {}).get("created_task_id")
        lines = [render(
            f"resolved signal {signal.get('id') or ''} "
            f"(state={signal.get('state', '?')}).")]
        if created:
            lines.append(render(muted(f"created task: {created}")))
        return "\n".join(lines)
    return render_attention(payload, verbosity)


register(
    Command(
        name="attention",
        help="Problems + alerts (read), or resolve a signal.",
        call=_call,
        render=_render,
        params=(
            Param("sub", "(read) | resolve <signal_id>", default=""),
            Param("a", "resolve: the signal id.", default=None),
            Param("state", "read: filter by state (e.g. open)."),
            Param("action", "resolve: the resolution action.", is_flag=False),
            Param("suggestion-id", "resolve: apply a specific suggestion.",
                  is_flag=False),
            Param("correction-file", "resolve: read correction text from this file.",
                  is_flag=False),
            Param("watch", "re-render the read view on the poll loop", is_flag=True),
            Param("yes", "Skip the confirmation prompt (required non-interactively).",
                  is_flag=True),
        ),
    )
)
