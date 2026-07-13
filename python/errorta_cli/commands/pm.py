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
from typing import Any, Callable

from ..client import SidecarClient
from ..errors import CliError
from ..registry import Command, Param, register, render_json
from ..render import is_no_project, muted, no_project, render
from ..render.pm import render_chat_entries, render_pm, thread_entries
from ..session import Context
from . import _base, _mutate

_READ_SUBS = ("chat", "changes")
_MUTATION_SUBS = ("ask", "control", "accept", "decline")

# pm-ask is a synchronous PM model turn: the sidecar waits up to ~120s (90s
# default, capped at 120) for the reply (coding.py:1747-1750). The client's 30s
# default would abort it, so this path gets a longer per-call timeout (+ margin).
_PM_ASK_TIMEOUT = 130.0

ReadLine = Callable[[str], str]
Write = Callable[[str], None]


def _ask_turn(client: SidecarClient, pid: str | None, question: str) -> dict[str, Any]:
    """One synchronous PM-ask turn (with the extended timeout). Returns the raw
    route result stamped ``_kind='ask'`` (shared by one-shot and interactive)."""
    result = client.post_json(
        f"/coding/projects/{pid}/pm-ask",
        json={"message": question}, timeout=_PM_ASK_TIMEOUT) or {}
    result["_kind"] = "ask"
    return result


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

    # -- interactive conversation (`pm chat -i` / bare `pm -i`) ---------------
    # `--interactive` maps via the flag param; `-i` (single dash) is NOT parsed
    # as a flag, so the generic positional parser drops it into whichever slot
    # its position lands in — `sub` (`pm -i`), then the `a` / `actions`
    # positionals (`pm chat -i`, `pm chat q -i`), then `_extra`. Detect it in any
    # slot and scrub the mis-read token so it isn't treated as a sub / question /
    # actions payload.
    _slots = ("sub", "a", "actions")
    interactive = (bool(args.get("interactive")) or "-i" in (args.get("_extra") or [])
                   or any(args.get(s) == "-i" for s in _slots))
    for s in _slots:
        if args.get(s) == "-i":
            args[s] = None
    sub = str(args.get("sub") or "chat").lower()
    if interactive:
        if ctx.json_mode or not _mutate.is_interactive():
            raise CliError(
                "interactive PM chat needs a terminal — use `pm ask \"<question>\"` "
                "(or `pm control`) for scripting / --json.",
                code="interactive_requires_tty")
        run_pm_chat(client, ctx, read_line=_default_read_line, write=_default_write)
        return {"_kind": "interactive_done"}

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
    return _ask_turn(client, pid, question)


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
    if kind == "interactive_done":
        return ""  # the loop did its own IO; nothing left to render
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


# --------------------------------------------------------------------------- #
# Interactive conversation (Item 1) — a back-and-forth loop over the same
# pm-ask / interject routes the one-shot commands use.
# --------------------------------------------------------------------------- #

_META_VERBS = {"/exit", "/quit", "/changes", "/help"}

_CHAT_BANNER = (
    "Talking to the PM. Type a question and press enter; prefix a line with `!` "
    "to send a directive instead. `/changes` review pending PM changes, `/help`, "
    "`/exit` (or Ctrl-D) to leave."
)


def _default_read_line(prompt: str) -> str:  # pragma: no cover — real terminal IO
    return input(prompt)


def _default_write(text: str) -> None:  # pragma: no cover — real terminal IO
    print(text)


def _classify_line(line: str) -> tuple[str, str]:
    """Map a raw input line to ``(kind, payload)`` where kind is
    ``meta`` / ``directive`` / ``question``. Grammar (documented in the banner):
    a leading meta-verb (`/exit` etc.) is meta; a leading ``!`` is a directive
    (``\\!`` escapes it back to a literal question); everything else — including a
    question that merely starts with ``/`` (e.g. a path) — is a question."""
    if not line.strip():                    # defensive: empty → treat as question
        return "question", line
    first = line.split(maxsplit=1)[0].lower()
    if first in _META_VERBS:
        return "meta", first.lstrip("/")
    if line.startswith("\\!"):          # escaped: a question that starts with '!'
        return "question", line[1:]
    if line.startswith("!"):
        return "directive", line[1:].lstrip()
    return "question", line


def _render_ask_reply(result: dict[str, Any]) -> str:
    """Render one pm-ask result for the interactive loop. Branch on
    ``answered is False`` (two shapes: unconfigured — no ``error`` key — and
    pm_unreachable) so a non-answer never KeyErrors or looks like a real reply."""
    reply = (result or {}).get("reply") or {}
    message = reply.get("message") or "(no reply)"
    if result.get("answered") is False:
        lines = [render(muted(f"PM: {message}"))]
        if reply.get("kind") == "unconfigured":
            lines.append(render(muted("  assemble a team first: errorta team apply --yes")))
        return "\n".join(lines)
    lines = [render(f"PM: {message}")]
    lines += _applied_refusal_lines(result)
    if (result or {}).get("applied"):
        lines.append(render(muted("  the PM changed config — review with `pm changes`.")))
    return "\n".join(lines)


def _deliver_directive(client: SidecarClient, pid: str | None, directive: str) -> str:
    """Send an authoritative directive (interject) and render the confirmation."""
    if not directive:
        return render(muted("(empty directive — nothing sent)"))
    result = client.post_json(
        f"/coding/projects/{pid}/interject", json={"message": directive}) or {}
    lines = [render(muted("directive delivered — the PM picks it up on its next plan turn."))]
    lines += _applied_refusal_lines(result)
    return "\n".join(lines)


def run_pm_chat(client: SidecarClient, ctx: Context, *,
                read_line: ReadLine, write: Write) -> None:
    """Interactive PM conversation loop (Item 1). ``read_line(prompt)`` returns a
    line (raising ``EOFError`` on Ctrl-D); ``write(text)`` prints one block.
    Reuses the exact pm-ask / interject / pm-changes routes the one-shot commands
    call — pure orchestration, no new route logic. Returns when the user exits."""
    pid = ctx.project_id
    _mutate.guard_sole_owner(ctx)
    chat = dict(client.get_json(f"/coding/projects/{pid}/pm-chat") or {})
    chat["_sub"] = "chat"
    write(render_pm(chat, ctx.verbosity))
    write(render(muted(_CHAT_BANNER)))
    while True:
        try:
            raw = read_line("pm ▸ ")
        except (EOFError, KeyboardInterrupt):
            write(render(muted("leaving PM chat.")))
            return
        line = raw.strip()
        if not line:
            continue
        kind, payload = _classify_line(line)
        if kind == "meta":
            if payload in ("exit", "quit"):
                write(render(muted("leaving PM chat.")))
                return
            if payload == "changes":
                changes = dict(client.get_json(f"/coding/projects/{pid}/pm-changes") or {})
                changes["_sub"] = "changes"
                write(render_pm(changes, ctx.verbosity))
            else:  # help
                write(render(muted(_CHAT_BANNER)))
            continue
        try:
            if kind == "directive":
                write(_deliver_directive(client, pid, payload))
            else:
                write(_render_ask_reply(_ask_turn(client, pid, payload)))
        except KeyboardInterrupt:
            # Ctrl-C during an in-flight turn aborts THIS turn, not the mode. The
            # server already recorded the user turn; the reply is simply lost to
            # the client (it surfaces in the transcript on the next `pm chat`).
            write(render(muted("(interrupted — the PM may still be replying; try `pm chat`)")))
        except CliError as exc:
            write(render(muted(f"error: {exc.message}")))


register(
    Command(
        name="pm",
        help="PM: read (`pm chat`/`pm changes`), converse (`pm chat -i` / "
             "`pm \"<q>\"`), or steer (control / accept / decline).",
        call=_call,
        render=_render,
        params=(
            Param("sub", "chat | changes | ask | control | accept | decline | "
                  "<question>", default="chat"),
            Param("a", "change_id / question / directive (single sub arg).",
                  default=None),
            Param("actions", "control: a JSON array of control-actions.",
                  is_flag=False),
            Param("interactive", "chat: open a back-and-forth PM conversation "
                  "(TTY only).", is_flag=True),
            Param("watch", "re-render a read view on the poll loop", is_flag=True),
            Param("yes", "Skip the confirmation prompt (required non-interactively).",
                  is_flag=True),
        ),
        # F158: `pm chat --watch` tails the transcript; `pm changes --watch` stays
        # a snapshot; steering subs are rejected by _reject_watched_mutation.
        watch_mode_fn=lambda a: (
            "stream" if str(a.get("sub") or "chat").lower() == "chat" else "snapshot"),
        stream_entries_fn=thread_entries,
        stream_render_fn=render_chat_entries,
    )
)
