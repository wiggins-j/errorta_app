"""Turn views (``GET /turns?limit=`` → ``turns``; ``turn`` adds ``.../composition``).

Turn: ``{turn_id, role, member_id, task_id, prompt, response, outcome, reason,
parse_ok, duration_ms, at, model_assignment, route_id, usage{...}, composition}``.
Composition (Context Report): ``{composition{sent_total, categories}, cli_overhead_tokens, note}``.

Prompt/response are server-capped verbatim; we cap again defensively and never
surface secrets — the transcript body is shown only in the deep ``turn`` view.
"""
from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text

from . import heading, muted, render, role_style, truncate, ts

_OUTCOME_STYLE = {
    "accepted": "cli.ok",
    "approved": "cli.ok",
    "rejected": "cli.bad",
    "error": "cli.bad",
    "repaired": "cli.warn",
}

_BODY_CAP = 1200


def render_turns(payload: Any, verbosity: Any) -> str:
    turns = (payload or {}).get("turns") or []
    if not turns:
        return render(muted("(no turns)"))
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("time", style="cli.muted", no_wrap=True)
    table.add_column("turn", style="cli.muted", no_wrap=True)
    table.add_column("role", no_wrap=True)
    table.add_column("route", no_wrap=True)
    table.add_column("outcome", no_wrap=True)
    table.add_column("tok", justify="right", no_wrap=True)
    for t in turns:
        usage = t.get("usage") if isinstance(t.get("usage"), dict) else {}
        tok = usage.get("input_tokens", 0) or 0
        out = usage.get("output_tokens", 0) or 0
        outcome = str(t.get("outcome") or "")
        table.add_row(
            ts(t.get("at")),
            str(t.get("turn_id") or ""),
            Text(str(t.get("role") or ""), style=role_style(t.get("role"))),
            truncate(t.get("route_id"), 24),
            Text(outcome, style=_OUTCOME_STYLE.get(outcome, "white")),
            f"{int(tok) + int(out):,}",
        )
    return render(table)


def render_turn_detail(payload: Any, verbosity: Any) -> str:
    """The deep ``turn <task> <turn>`` view: header + transcript + Context Report."""
    turn = (payload or {}).get("turn")
    composition = (payload or {}).get("composition") or {}
    if not turn:
        return render(muted("(turn not found)"))
    parts = [heading(f"turn {turn.get('turn_id') or ''}")]
    meta = Table(show_edge=False, pad_edge=False, box=None, show_header=False)
    meta.add_column("k", style="cli.key", no_wrap=True)
    meta.add_column("v")
    for key in ("role", "member_id", "task_id", "route_id", "outcome", "reason", "duration_ms"):
        if turn.get(key) not in (None, ""):
            meta.add_row(key, str(turn.get(key)))
    parts.append(meta)

    prompt = turn.get("prompt")
    response = turn.get("response")
    if prompt:
        parts.append(heading("prompt"))
        parts.append(Text(truncate_body(prompt)))
    if response:
        parts.append(heading("response"))
        parts.append(Text(truncate_body(response)))

    comp = composition.get("composition") if isinstance(composition, dict) else None
    if isinstance(comp, dict) and comp.get("categories"):
        parts.append(heading("context report"))
        ctable = Table(show_edge=False, pad_edge=False, box=None)
        ctable.add_column("category", style="cli.key", no_wrap=True)
        ctable.add_column("tokens", justify="right", no_wrap=True)
        for cat in comp.get("categories") or []:
            if isinstance(cat, dict):
                ctable.add_row(
                    str(cat.get("name") or cat.get("category") or "?"),
                    f"{int(cat.get('tokens') or cat.get('sent') or 0):,}",
                )
        parts.append(ctable)
        sent_total = comp.get("sent_total")
        if sent_total is not None:
            parts.append(muted(f"sent_total: {int(sent_total):,}"))
    overhead = composition.get("cli_overhead_tokens") if isinstance(composition, dict) else None
    if overhead is not None:
        parts.append(muted(f"cli overhead (vendor-managed): ~{overhead}"))
    note = composition.get("note") if isinstance(composition, dict) else None
    if note:
        parts.append(muted(str(note)))
    return render(*parts)


def truncate_body(value: Any) -> str:
    text = str(value or "")
    if len(text) <= _BODY_CAP:
        return text
    return text[:_BODY_CAP] + f"\n… [+{len(text) - _BODY_CAP} chars]"
