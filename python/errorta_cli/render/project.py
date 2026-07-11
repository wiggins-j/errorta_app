"""Project-lifecycle + steering views (F147 §8.1, §8.3, §9 "Project").

Renders the S5 surfaces: the project list (with the derived ``list_status`` /
``list_status_reason`` the app shows), a single project's detail (North Star /
Definition of Done / phase / delivery paths / runtime evidence), the GitHub
auth-status probe, the Current Focus list, and a North Star proposal.

Golden invariant #5: every renderer SELECTS the fields it surfaces — it never
dumps the raw payload. The only way to see raw bytes is the explicit ``--json``
bypass, which the registry short-circuits before a renderer runs.
"""
from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text

from . import heading, muted, render, truncate

_LIST_STATUS_STYLE = {
    "running": "cli.warn",
    "needs attention": "cli.bad",
    "active": "cli.ok",
    "paused": "cli.muted",
    "done": "cli.ok",
    "failed": "cli.bad",
}


def render_projects(payload: Any, verbosity: Any) -> str:
    projects = (payload or {}).get("projects") or []
    if not projects:
        return render(muted("no projects yet — create one with: errorta new <id>"))
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("id", style="cli.key", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("reason", style="cli.muted")
    table.add_column("north star", style="cli.muted")
    for proj in projects:
        status = str(proj.get("list_status") or proj.get("status") or "?")
        table.add_row(
            str(proj.get("id") or "?"),
            Text(status, style=_LIST_STATUS_STYLE.get(status, "white")),
            truncate(proj.get("list_status_reason") or "", 28),
            truncate(proj.get("north_star") or "", 48),
        )
    return render(table)


def render_project(payload: Any, verbosity: Any) -> str:
    project = _unwrap(payload)
    if not project:
        return render(muted("project not found"))
    lines: list[Any] = [Text(f"project: {project.get('id', '?')}", style="cli.key")]
    status = str(project.get("list_status") or project.get("status") or "?")
    lines.append(Text(f"status:  {status}",
                      style=_LIST_STATUS_STYLE.get(status, "white")))
    phase = project.get("phase")
    if phase:
        lines.append(muted(f"phase:   {phase}"))
    target = project.get("target")
    if target:
        lines.append(muted(f"target:  {target}"))
    lines.extend(_north_star_lines(project))
    for label, key in (("repo:    ", "repo_path"),
                       ("deliver: ", "planned_delivery_dir"),
                       ("root:    ", "delivery_root")):
        val = project.get(key)
        if val:
            lines.append(muted(f"{label}{val}"))
    src = project.get("import_source") or {}
    if isinstance(src, dict) and src.get("kind"):
        origin = src.get("origin_url") or ""
        lines.append(muted(f"import:  {src.get('kind')} {origin}".rstrip()))
    lines.extend(_runtime_evidence_lines(project.get("runtime_evidence") or {}))
    return render(*lines)


def _north_star_lines(project: dict[str, Any]) -> list[Any]:
    lines: list[Any] = []
    ns = str(project.get("north_star") or "").strip()
    dod = str(project.get("definition_of_done") or "").strip()
    wr = str(project.get("work_request") or "").strip()
    lines.append(heading("North Star"))
    lines.append(Text(ns or "(not set)") if ns else muted("(not set)"))
    if dod:
        lines.append(heading("Definition of Done"))
        lines.append(Text(dod))
    if wr:
        lines.append(muted(f"current focus: {truncate(wr, 80)}"))
    return lines


def _runtime_evidence_lines(evidence: dict[str, Any]) -> list[Any]:
    results = evidence.get("results") or []
    if not results and not evidence.get("current_head"):
        return []
    fresh = bool(evidence.get("any_fresh_pass"))
    style = "cli.ok" if fresh else "cli.muted"
    label = "a fresh launch/test passed" if fresh else "no fresh pass"
    return [Text(f"runtime: {label} ({len(results)} result(s))", style=style)]


def render_north_star(payload: Any, verbosity: Any) -> str:
    """``north-star show`` — just the NS + DoD (a focused view of the project)."""
    project = _unwrap(payload)
    if not project:
        return render(muted("project not found"))
    lines: list[Any] = [Text(f"project: {project.get('id', '?')}", style="cli.key")]
    lines.extend(_north_star_lines(project))
    return render(*lines)


def render_proposal(payload: Any, verbosity: Any) -> str:
    proposal = (payload or {}).get("proposal") if isinstance(payload, dict) else None
    if not isinstance(proposal, dict):
        return render(muted("no North Star proposal yet — run an import + scan first."))
    lines: list[Any] = [heading("Proposed North Star")]
    lines.append(Text(str(proposal.get("north_star") or "(empty)")))
    dod = str(proposal.get("definition_of_done") or "").strip()
    if dod:
        lines.append(heading("Proposed Definition of Done"))
        lines.append(Text(dod))
    if proposal.get("accepted"):
        lines.append(Text("accepted", style="cli.ok"))
    else:
        lines.append(muted("accept with: errorta north-star accept --yes"))
    return render(*lines)


def render_focus_list(payload: Any, verbosity: Any) -> str:
    focuses = (payload or {}).get("focuses") or []
    if not focuses:
        return render(muted("no Current Focus goals — add one with: errorta focus add \"...\""))
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("#", style="cli.muted", justify="right", no_wrap=True)
    table.add_column("id", style="cli.key", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("title", style="cli.muted")
    for i, focus in enumerate(focuses, 1):
        status = str(focus.get("status") or "active")
        table.add_row(
            str(i),
            str(focus.get("id") or "?"),
            Text(status, style="cli.ok" if status == "active" else "cli.muted"),
            truncate(focus.get("title") or "", 60),
        )
    return render(table)


def render_focus_one(payload: Any, verbosity: Any) -> str:
    focus = (payload or {}).get("focus") if isinstance(payload, dict) else None
    if not isinstance(focus, dict):
        return render(muted("focus not found"))
    over = bool((payload or {}).get("over_soft_cap"))
    lines: list[Any] = [
        Text(f"focus {focus.get('id', '?')}: {focus.get('title', '')}", style="cli.key"),
        muted(f"status: {focus.get('status', 'active')}"),
    ]
    body = str(focus.get("body") or "").strip()
    if body:
        lines.append(Text(body))
    if over:
        lines.append(Text("note: over the active-focus soft cap", style="cli.warn"))
    return render(*lines)


def render_auth_status(payload: Any, verbosity: Any) -> str:
    """``import github`` (no url) — GitHub auth probe. NEVER prints a token."""
    data = payload if isinstance(payload, dict) else {}
    present = bool(data.get("gh_present"))
    login = data.get("login")
    keychain = bool(data.get("token_in_keychain"))
    lines: list[Any] = [heading("GitHub auth")]
    lines.append(Text(f"gh cli:   {'present' if present else 'not found'}",
                      style="cli.ok" if present else "cli.bad"))
    lines.append(Text(f"logged in: {login}" if login else "logged in: no",
                      style="cli.ok" if login else "cli.muted"))
    # A boolean only — the token itself is never returned by the route or shown.
    lines.append(muted(f"token in keychain: {'yes' if keychain else 'no'}"))
    if not present:
        lines.append(muted("install + auth the GitHub CLI (gh) to import from GitHub."))
    return render(*lines)


def render_branches(payload: Any, verbosity: Any) -> str:
    data = payload if isinstance(payload, dict) else {}
    if not data.get("ok"):
        return render(muted(f"could not list branches ({data.get('error', 'unknown')}); "
                            "pass --branch to pick one directly."))
    branches = data.get("branches") or []
    default = data.get("default_branch")
    lines: list[Any] = [heading("branches")]
    for b in branches[:50]:
        mark = " (default)" if b == default else ""
        lines.append(Text(f"  {b}{mark}"))
    return render(*lines)


def _unwrap(payload: Any) -> dict[str, Any]:
    """Accept either a raw project dict or a ``{"project": {...}}`` envelope."""
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("project")
    if isinstance(inner, dict):
        return inner
    return payload
