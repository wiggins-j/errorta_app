"""``wizard`` — conversational project + team setup (F147 §7.3).

Drives the F145 AI Wizard over its existing routes (coding.py:1754-1828):
``GET /coding/wizard/models`` → ``POST /coding/wizard/start {model_route}`` →
``POST /coding/wizard/{sid}/message {message}`` (looped) →
``POST /coding/wizard/{sid}/create {project_id, delivery_root?}``. A terminal chat
that drafts a North Star + team and creates a runnable project — the fastest
zero-to-running path for a standalone user.

The chat loop uses injectable IO seams (``read_line`` / ``write``) so the
start→message→create sequence is driven deterministically in tests without
patching builtins (mirrors ``runstream``'s ``sleep``/``emit`` seams). In-loop
control words: a message ``:create <project_id>`` finalizes + creates (a mutation
— sole-owner + ``--yes`` gate), ``:quit`` aborts, anything else is a chat turn.

Non-interactive with no ``--model`` (and every ``--json`` call) short-circuits to
listing the available models — a real route call, never a hung REPL.
"""
from __future__ import annotations

from typing import Any, Callable

from ..client import SidecarClient
from ..errors import CliError
from ..registry import Command, Param, register, render_json
from ..render import muted, render
from ..render.wizard import render_wizard
from ..session import Context
from . import _mutate

ReadLine = Callable[[str], str]
Write = Callable[[str], None]


def _default_read_line(prompt: str) -> str:  # pragma: no cover — real REPL IO
    try:
        return input(prompt)
    except EOFError:
        return ":quit"


def _default_write(text: str) -> None:  # pragma: no cover — real REPL IO
    print(text)


def run_wizard(
    client: SidecarClient,
    ctx: Context,
    args: dict[str, Any],
    *,
    read_line: ReadLine,
    write: Write,
) -> dict[str, Any]:
    """Run the interactive chat loop; return a terminal sentinel for the renderer."""
    model = str(args.get("model") or "").strip()
    # POST /coding/wizard/start (coding.py:1763).
    started = client.post_json("/coding/wizard/start", json={"model_route": model})
    session_id = str((started or {}).get("session_id") or "")
    if not session_id:
        raise CliError("wizard did not return a session", code="wizard_no_session")
    opening = str((started or {}).get("reply") or "")
    if opening:
        write(opening)

    default_project = str(args.get("project") or "").strip()
    delivery_root = str(args.get("delivery-root") or "").strip() or None

    while True:
        line = read_line("you> ").strip()
        if line in (":quit", ":q", ""):
            return {"_kind": "aborted"}
        if line.startswith(":create") or line.startswith(":done"):
            parts = line.split(maxsplit=1)
            project_id = parts[1].strip() if len(parts) > 1 else default_project
            if not project_id:
                write("usage: :create <project_id>")
                continue
            return _create(client, ctx, args, session_id, project_id, delivery_root)
        # POST /coding/wizard/{sid}/message (coding.py:1786).
        turn = client.post_json(
            f"/coding/wizard/{session_id}/message", json={"message": line}
        )
        reply = str((turn or {}).get("reply") or "")
        if reply:
            write(reply)
        missing = (turn or {}).get("missing") or []
        if (turn or {}).get("ready"):
            write("(charter ready — type ':create <project_id>' to build it)")
        elif missing:
            write("still needed: " + ", ".join(str(m) for m in missing))


def _create(client: SidecarClient, ctx: Context, args: dict[str, Any],
            session_id: str, project_id: str, delivery_root: str | None) -> dict[str, Any]:
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"create project '{project_id}'",
                           note="creates a project + team from the charter",
                           interactive_prompt=False):
        return {"_kind": "aborted"}
    body: dict[str, Any] = {"project_id": project_id}
    if delivery_root:
        body["delivery_root"] = delivery_root
    # POST /coding/wizard/{sid}/create (coding.py:1828).
    created = client.post_json(f"/coding/wizard/{session_id}/create", json=body)
    return {"_kind": "created", "project_id": project_id, "created": created}


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    # GET /coding/wizard/models (coding.py:1754) — always the first read.
    models = client.get_json("/coding/wizard/models")
    model = str(args.get("model") or "").strip()
    # A --json call or a non-interactive session without a preselected model can't
    # run a chat loop — surface the model list (a real route call, no hang).
    if ctx.json_mode or (not _mutate.is_interactive() and not model):
        return {"_kind": "models", "models": models}
    return run_wizard(client, ctx, args, read_line=_default_read_line, write=_default_write)


def _render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload.get("models") if (payload or {}).get("_kind") == "models"
                           else payload)
    kind = (payload or {}).get("_kind")
    if kind == "aborted":
        return render(muted("wizard: aborted — no project created."))
    if kind == "created":
        pid = payload.get("project_id")
        created = payload.get("created") or {}
        # Only point at `run` when the route actually confirmed setup (a team
        # resolved). With no runnable provider it returns run_setup_confirmed=False
        # + warnings; echoing "run it" there just sends the user into exit-12.
        if created.get("run_setup_confirmed"):
            return render(muted(f"project '{pid}' created — run it with: errorta run --yes"))
        msg = f"project '{pid}' created, but it's not ready to run yet."
        for w in (created.get("warnings") or []):
            msg += f"\n  - {w}"
        msg += ("\nConnect a provider and assign a team, then run:"
                "\n  errorta connect <provider>   errorta team apply --yes   errorta run --yes")
        return render(muted(msg))
    return render_wizard(payload)


register(
    Command(
        name="wizard",
        help="Conversational project + team setup (AI Wizard).",
        call=_call,
        render=_render,
        params=(
            Param("model", "Model route to power the wizard (skips the picker).",
                  is_flag=False),
            Param("project", "Default project id for ':create'.", is_flag=False),
            Param("delivery-root", "Greenfield delivery parent dir.", is_flag=False),
            Param("yes", "Skip the create confirmation (required non-interactively).",
                  is_flag=True),
        ),
    )
)
