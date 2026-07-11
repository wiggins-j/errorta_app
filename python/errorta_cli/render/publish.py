"""Publish views (F147 §8.6) — targets / events / auth-status / export / PR.

Golden invariant #4: NEVER render a gh token. ``auth-status`` surfaces only the
booleans + login the route returns (``gh_present`` / ``login`` /
``token_in_keychain``) — the route itself never returns a token, and this view
selects fields, so even a future token field would not be printed.
"""
from __future__ import annotations

from typing import Any

from rich.table import Table

from . import heading, muted, render, truncate


def _targets_table(targets: list[dict[str, Any]]) -> Any:
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("target", style="cli.key", no_wrap=True)
    table.add_column("kind", no_wrap=True)
    table.add_column("repo")
    table.add_column("state", style="cli.muted", no_wrap=True)
    for t in targets:
        table.add_row(
            str(t.get("target_id") or ""),
            str(t.get("kind") or ""),
            truncate(t.get("repo_url") or t.get("repo") or "", 48),
            str(t.get("state") or ""),
        )
    return table


def render_targets(payload: Any) -> str:
    targets = (payload or {}).get("targets") or []
    if not targets:
        return render(heading("Publish targets"),
                      muted("(no publish targets yet)"))
    return render(heading("Publish targets"), _targets_table(targets))


def render_events(payload: Any) -> str:
    events = (payload or {}).get("events") or []
    if not events:
        return render(heading("Publish events"), muted("(no publish events yet)"))
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("kind", style="cli.key", no_wrap=True)
    table.add_column("state", no_wrap=True)
    table.add_column("detail")
    for e in events:
        table.add_row(
            str(e.get("kind") or ""),
            str(e.get("state") or ""),
            truncate(e.get("error") or e.get("pr_url") or e.get("commit_sha") or "", 60),
        )
    return render(heading("Publish events"), table)


def render_auth(payload: Any) -> str:
    """Never prints a token — only presence booleans + login (invariant #4)."""
    auth = (payload or {}).get("auth") or {}
    present = bool(auth.get("gh_present"))
    login = auth.get("login")
    tok = bool(auth.get("token_in_keychain"))
    lines = [heading("GitHub publish auth")]
    lines.append(render_kv("gh present", "yes" if present else "no"))
    lines.append(render_kv("logged in as", str(login) if login else "(not logged in)"))
    lines.append(render_kv("device-flow token", "in keychain" if tok else "none"))
    return render(*lines)


def render_kv(key: str, value: str) -> Any:
    from rich.text import Text
    t = Text()
    t.append(f"{key}: ", style="cli.key")
    t.append(value)
    return t


def render_export(payload: Any) -> str:
    export = (payload or {}).get("export") or {}
    kind = str(export.get("kind") or "")
    path = export.get("path")
    lines = [heading(f"Manual export ({kind})")]
    if path:
        lines.append(render_kv("path", str(path)))
        lines.append(muted(f"open it yourself, e.g.:  open {path}"))
    hint = export.get("run_hint") or export.get("command")
    if hint:
        lines.append(muted(f"run: {hint}"))
    if kind == "patch" and not path:
        lines.append(muted("(no changes to export — patch is empty)"))
    return render(*lines)


def render_pr_result(payload: Any) -> str:
    result = (payload or {}).get("result") or {}
    url = result.get("pr_url") or result.get("url") or result.get("html_url")
    branch = result.get("branch")
    lines = [heading("Pull request opened")]
    if url:
        lines.append(render_kv("pr", str(url)))
    if branch:
        lines.append(render_kv("branch", str(branch)))
    if not url:
        lines.append(muted("(PR flow completed; see --json for the full result)"))
    return render(*lines)


def render_new_repo_result(payload: Any) -> str:
    result = (payload or {}).get("result") or {}
    url = result.get("repo_url") or result.get("html_url") or result.get("url")
    local_only = result.get("local_only")
    lines = [heading("New repository")]
    if url:
        lines.append(render_kv("repo", str(url)))
    if local_only:
        lines.append(muted("local-only: created a local git repo (not pushed to GitHub)"))
    if not url and not local_only:
        lines.append(muted("(repo flow completed; see --json for the full result)"))
    return render(*lines)
