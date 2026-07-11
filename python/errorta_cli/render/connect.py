"""Rich rendering for ``connect`` (F147 §7.1, §9).

Renderers SELECT fields — they never dump the raw payload (invariant #4/#5). In
particular the API-key value is NEVER available here: the ``PUT`` response is
``provider_keys.mask_all()`` (``…<last4>`` previews only), so there is no raw key
to leak even by accident.
"""
from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text

from . import heading, muted, render


def _yn(value: Any) -> Text:
    if value is True:
        return Text("yes", style="cli.ok")
    if value is False:
        return Text("no", style="cli.bad")
    return Text("?", style="cli.muted")


def _test_line(test: Any) -> Text:
    """One line for a ``POST .../test`` result ``{ok, detail, state?, remediation?}``."""
    data = test or {}
    ok = data.get("ok")
    line = Text("  test: ", style="cli.muted")
    line.append("connected" if ok else "not connected", style="cli.ok" if ok else "cli.bad")
    state = str(data.get("state") or "")
    if state:
        line.append(f" ({state})", style="cli.muted")
    detail = str(data.get("detail") or "")
    if detail and not ok:
        line.append(f" — {detail}", style="cli.muted")
    return line


def render_connect(payload: Any) -> str:
    kind = (payload or {}).get("_kind")
    if kind == "aborted":
        return render(muted("aborted — nothing written."))
    if kind == "status":
        return _render_status(payload)
    if kind == "api":
        return _render_api(payload)
    if kind == "cli":
        return _render_cli(payload)
    if kind == "custom":
        return _render_custom(payload)
    if kind == "ollama":
        return _render_ollama(payload)
    return render(muted("connect: nothing to show"))


def _render_status(payload: Any) -> str:
    providers = ((payload or {}).get("providers") or {}).get("providers") or []
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("provider", style="cli.key", no_wrap=True)
    table.add_column("configured", no_wrap=True)
    table.add_column("connected", no_wrap=True)
    for p in providers:
        if not isinstance(p, dict):
            continue
        table.add_row(
            str(p.get("provider_class") or ""),
            _yn(p.get("configured")),
            _yn(p.get("connected")) if "connected" in p else muted("n/a"),
        )
    if not providers:
        return render(muted("(no providers registered)"))
    return render(heading("Providers"), table)


def _masked_line(masked: Any, provider: str) -> Text:
    entry = (masked or {}).get(provider) or {}
    line = Text(f"{provider}: ", style="cli.key")
    if entry.get("configured"):
        line.append("configured ", style="cli.ok")
        line.append(str(entry.get("key_preview") or ""), style="cli.muted")
    else:
        line.append("not configured", style="cli.warn")
    return line


def _render_api(payload: Any) -> str:
    provider = str((payload or {}).get("provider") or "")
    return render(
        _masked_line(payload.get("masked"), provider),
        _test_line(payload.get("test")),
    )


def _render_cli(payload: Any) -> str:
    provider = str((payload or {}).get("provider") or "")
    status = (payload or {}).get("status") or {}
    lines: list[Any] = [Text(provider, style="cli.key")]
    # Select a small allowlist of detect fields (whatever the handler surfaced).
    detail = Text("  ", style="cli.muted")
    for label in ("source", "binary", "path", "version"):
        val = status.get(label)
        if val:
            detail.append(f"{label}={val}  ", style="cli.muted")
    if str(detail.plain).strip():
        lines.append(detail)
    lines.append(_test_line(payload.get("test")))
    login = (payload or {}).get("login")
    if isinstance(login, dict):
        argv = login.get("login_argv") or []
        if argv:
            lines.append(muted("  login: " + " ".join(str(a) for a in argv)))
        if login.get("install_command"):
            lines.append(muted("  install: " + str(login.get("install_command"))))
    return render(*lines)


def _render_custom(payload: Any) -> str:
    alias = str((payload or {}).get("alias") or "")
    masked = (payload or {}).get("masked") or {}
    entry = None
    for c in masked.get("custom") or []:
        if isinstance(c, dict) and c.get("alias") == alias:
            entry = c
            break
    line = Text(f"custom '{alias}': ", style="cli.key")
    if entry:
        line.append("configured ", style="cli.ok")
        line.append(f"{entry.get('base_url', '')} [{entry.get('api_style', '')}] ",
                    style="cli.muted")
        line.append(str(entry.get("key_preview") or ""), style="cli.muted")
    else:
        line.append("saved", style="cli.ok")
    return render(line, _test_line(payload.get("test")))


def _render_ollama(payload: Any) -> str:
    routes = ((payload or {}).get("routes") or {}).get("routes") or []
    lines: list[Any] = [heading("Ollama (local)")]
    host = str((payload or {}).get("host_hint") or "")
    lines.append(muted(
        "the sidecar reads ERRORTA_OLLAMA_HOST at call time; set it before the "
        "sidecar starts (the CLI can't change a running sidecar's env)."
    ))
    if host:
        lines.append(muted(f"  suggested: export ERRORTA_OLLAMA_HOST={host}"))
    if routes:
        table = Table(show_edge=False, pad_edge=False, box=None)
        table.add_column("route", style="cli.key", no_wrap=True)
        table.add_column("label")
        for r in routes:
            if isinstance(r, dict):
                table.add_row(str(r.get("route_id") or ""), str(r.get("label") or ""))
        lines.append(table)
    else:
        lines.append(muted("  (no local routes — is ollama running?)"))
    return render(*lines)
