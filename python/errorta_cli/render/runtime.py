"""Runtime view (``GET /runtime/profiles`` + optional ``.../sessions/{sid}``).

Profile: ``{profile_id, kind, runtime_mode, working_dir, setup, start, stop,
health, ports, sandbox, ...}`` (``runtime.RuntimeProfile.to_dict``). Read-only —
runtime control (setup/start/stop) is S7.
"""
from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text

from . import heading, muted, render, truncate


def _argv(value: Any) -> str:
    if isinstance(value, list):
        if value and isinstance(value[0], list):  # list[argv]
            return "; ".join(" ".join(str(x) for x in step) for step in value)
        return " ".join(str(x) for x in value)
    return truncate(value, 60)


def render_runtime(payload: Any, verbosity: Any) -> str:
    profiles = (payload or {}).get("profiles") or []
    session = (payload or {}).get("session")
    parts = [heading("Runtime profiles")]
    if not profiles:
        parts.append(muted("(no runtime profiles — /runtime detect proposes some)"))
    else:
        table = Table(show_edge=False, pad_edge=False, box=None)
        table.add_column("profile", style="cli.key", no_wrap=True)
        table.add_column("kind", no_wrap=True)
        table.add_column("mode", no_wrap=True)
        table.add_column("start")
        table.add_column("sandbox", style="cli.muted", no_wrap=True)
        for p in profiles:
            table.add_row(
                str(p.get("profile_id") or ""),
                str(p.get("kind") or ""),
                str(p.get("runtime_mode") or ""),
                truncate(_argv(p.get("start")), 48),
                str(p.get("sandbox") or ""),
            )
        parts.append(table)

    if isinstance(session, dict) and session:
        parts.append(heading("session"))
        stable = Table(show_edge=False, pad_edge=False, box=None, show_header=False)
        stable.add_column("k", style="cli.key", no_wrap=True)
        stable.add_column("v")
        # Real ``RuntimeSession.to_dict`` fields (runtime.py): state/pgid/
        # allocated_ports (NOT status/pid/port/url). Join the port list.
        for key in ("session_id", "profile_id", "state", "pgid", "allocated_ports",
                    "sandbox_backend", "started_at", "ended_at", "exit_code", "error"):
            if key not in session:
                continue
            value = session.get(key)
            if value in (None, "", []):
                continue
            if key == "allocated_ports" and isinstance(value, list):
                value = ", ".join(str(p) for p in value)
            stable.add_row(key, str(value))
        parts.append(stable)
    return render(*parts)


# --------------------------------------------------------------------------- #
# S7 — runtime control views.
# --------------------------------------------------------------------------- #

def _kv(key: str, value: Any) -> Text:
    t = Text()
    t.append(f"{key}: ", style="cli.key")
    t.append(str(value))
    return t


def render_detect(payload: Any) -> str:
    proposed = (payload or {}).get("proposed") or []
    if not proposed:
        return render(heading("Runtime detect"),
                      muted("(nothing detected — no runnable entrypoint found)"))
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("profile", style="cli.key", no_wrap=True)
    table.add_column("kind", no_wrap=True)
    table.add_column("start")
    for p in proposed:
        table.add_row(str(p.get("profile_id") or ""), str(p.get("kind") or ""),
                      truncate(_argv(p.get("start")), 48))
    return render(heading("Runtime detect (proposed profiles)"), table)


def render_run(payload: Any) -> str:
    run = (payload or {}).get("run") or {}
    resolved = bool(run.get("resolved"))
    runnable = bool(run.get("runnable"))
    session = run.get("session")
    lines = [heading("Runtime run")]
    if not resolved:
        lines.append(muted("could not resolve how to run this project."))
        for item in run.get("looked_for") or []:
            lines.append(muted(f"  looked for: {item}"))
        return render(*lines)
    plan = run.get("plan") or {}
    lines.append(_kv("modality", plan.get("modality") or "?"))
    if not runnable:
        lines.append(Text(f"not runnable: {run.get('reason')}", style="cli.warn"))
        return render(*lines)
    if session:
        lines.append(Text("launched", style="cli.ok"))
        lines.append(_kv("session", session.get("session_id") or ""))
        lines.append(_kv("state", session.get("state") or ""))
        url = (payload or {}).get("_url")
        if url:
            lines.append(_kv("open", url))
            if (payload or {}).get("_opened"):
                lines.append(muted("opened in your browser."))
            else:
                lines.append(muted("visit that URL in your browser (the dev server "
                                   "may take a few seconds to compile)."))
    else:
        lines.append(muted("preview only — re-run with --go --yes to launch."))
        if run.get("requires_reduced_isolation_consent"):
            lines.append(muted("this launch needs --reduced-isolation consent."))
    return render(*lines)


def render_session_result(payload: Any, *, verb: str) -> str:
    session = (payload or {}).get("session") or {}
    lines = [heading(f"Runtime {verb}")]
    for key in ("session_id", "profile_id", "state", "allocated_ports", "exit_code",
                "error"):
        value = session.get(key)
        if value in (None, "", []):
            continue
        if key == "allocated_ports" and isinstance(value, list):
            value = ", ".join(str(p) for p in value)
        lines.append(_kv(key, value))
    if not session:
        lines.append(muted(f"{verb} completed."))
    return render(*lines)


def render_stopped(payload: Any) -> str:
    return render(heading("Runtime stop"),
                  Text("stopped" if (payload or {}).get("stopped") else "no live session",
                       style="cli.ok"))


def render_health(payload: Any) -> str:
    status = (payload or {}).get("health_status")
    return render(heading("Runtime health-check"), _kv("status", status))


def render_test(payload: Any) -> str:
    result = (payload or {}).get("result") or {}
    passed = bool(result.get("passed"))
    lines = [heading("Runtime test"),
             _kv("kind", result.get("kind") or ""),
             Text("passed" if passed else "failed",
                  style="cli.ok" if passed else "cli.bad")]
    ref = result.get("screenshot_ref") or result.get("screenshotRef")
    if ref:
        lines.append(_kv("screenshot", ref))
    detail = result.get("detail")
    if detail:
        lines.append(muted(truncate(detail, 120)))
    return render(*lines)


def render_logs(payload: Any) -> str:
    logs = (payload or {}).get("logs") or {}
    lines = logs.get("lines") or []
    parts = [heading("Runtime logs")]
    if not lines:
        parts.append(muted("(no log lines yet)"))
    else:
        for line in lines[-200:]:
            parts.append(Text(str(line)))
    return render(*parts)


def render_profile_saved(payload: Any) -> str:
    profile = (payload or {}).get("profile") or {}
    return render(heading("Runtime profile saved"),
                  _kv("profile", profile.get("profile_id") or ""),
                  _kv("kind", profile.get("kind") or ""),
                  _kv("start", truncate(_argv(profile.get("start")), 60)))


def render_evidence(payload: Any) -> str:
    project = (payload or {}).get("project") or {}
    evidence = project.get("runtime_evidence") or {}
    results = evidence.get("results") or []
    fresh = bool(evidence.get("any_fresh_pass"))
    lines = [heading("Runtime evidence")]
    lines.append(Text("a fresh launch/test passed" if fresh else "no fresh pass",
                      style="cli.ok" if fresh else "cli.muted"))
    if results:
        table = Table(show_edge=False, pad_edge=False, box=None)
        table.add_column("kind", style="cli.key", no_wrap=True)
        table.add_column("passed", no_wrap=True)
        table.add_column("head", style="cli.muted", no_wrap=True)
        for r in results:
            table.add_row(str(r.get("kind") or ""),
                          "yes" if r.get("passed") else "no",
                          str(r.get("head") or "")[:12])
        lines.append(table)
    # F146 delivery outcome (the accept/delivered marker on the project payload).
    delivered = project.get("delivered")
    if delivered is not None:
        lines.append(_kv("delivered", "yes" if delivered else "no"))
        if project.get("delivered_at"):
            lines.append(muted(f"delivered at {project.get('delivered_at')}"))
    return render(*lines)
