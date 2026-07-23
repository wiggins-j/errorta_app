"""``interject`` — send an authoritative directive into a (possibly running) team.

Grounded against ``routes/coding.py`` (verified this session):

* ``interject "<directive>"`` → ``POST /coding/projects/{id}/interject`` (coding.py:1494)
  body ``{"message": "<directive>", "artifact_id"?: "..."}`` → response
  ``{ok, interjection, applied, refusals, run_started}``.

This is the marquee **mid-run steering** command: the PM consumes the directive
on its next plan turn (the F049 pinned contract) AND the engine interprets it into
grounded control-actions (F145). It is NOT refused while a run is live — that is
the entire point. ``guard_sole_owner`` only refuses a FOREIGN desktop app; it never
blocks the CLI's own live run (golden invariant #6).

The directive is a single quoted positional argument (the S1 whitespace tokenizer
splits on spaces until quoted-arg parsing lands, same limitation as ``focus add``).
Correction text / file content is never passed via argv.
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, Param, register, render_json
from ..render import is_no_project, muted, no_project, render
from ..session import Context
from . import _base, _mutate


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    directive = str(args.get("a") or "").strip()
    if not directive:
        return _base.usage('interject "<directive>" [--artifact-id ID]')
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(
        ctx, args, "interject a directive to the PM",
        note="steers the team; the PM reads it on its next plan turn "
             "and may start/continue a run (spends model budget)",
        interactive_prompt=False,
    ):
        return {"_kind": "aborted"}
    body: dict[str, Any] = {"message": directive}
    artifact_id = args.get("artifact-id")
    if artifact_id:
        body["artifact_id"] = str(artifact_id)
    result = client.post_json(
        f"/coding/projects/{ctx.project_id}/interject", json=body) or {}
    return {"_kind": "interjected", **result}


def _render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    if is_no_project(payload):
        return no_project()
    usage = payload.get("_usage") if isinstance(payload, dict) else None
    if usage:
        return render(muted(f"usage: {usage}"))
    if (payload or {}).get("_kind") == "aborted":
        return render(muted("aborted — no directive sent."))
    applied = (payload or {}).get("applied") or []
    refusals = (payload or {}).get("refusals") or []
    run_started = bool((payload or {}).get("run_started"))
    lines = [render("directive delivered — the PM will pick it up on its next plan turn.")]
    if applied:
        lines.append(render(muted(f"applied {len(applied)} change(s) from the directive")))
    for r in refusals:
        if isinstance(r, dict):
            code = r.get("code") or "?"
            reason = r.get("reason") or ""
            # Compat guard for an older server: a start_run refused only because a
            # run is already live is benign (the directive was still delivered).
            if code == "start_failed" and "already in progress" in reason:
                continue
            lines.append(render(muted(f"refused: {code} — {reason}")))
    if run_started:
        lines.append(render("the directive started a run."))
    return "\n".join(lines)


register(Command(
    name="interject",
    help="Send an authoritative directive to the PM (works mid-run).",
    call=_call,
    render=_render,
    params=(
        Param("a", "The directive text (quote it).", default=None),
        Param("artifact-id", "Attach the directive to a governance artifact.",
              is_flag=False),
        Param("yes", "Skip the confirmation prompt (required non-interactively).",
              is_flag=True),
    ),
    mutating=True,
))
