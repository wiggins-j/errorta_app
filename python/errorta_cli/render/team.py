"""Team views (F147 §7.2, §9).

Three surfaces:

* ``render_draft`` — the CLI-local team draft the user is assembling (the members
  ``run-setup`` will consume).
* ``render_show`` — the ``team`` (show) view: the draft when present, else the
  read-only ``GET /model-usage`` *projection* (derived + lossy; banner says so).
* ``render_rooms`` — the Council room list (``GET /council/rooms``) a user can back
  the team with.

Renderers SELECT fields — never a raw-payload dump (invariant #4/#5).
"""
from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text

from . import heading, muted, render, role_style, truncate


def _draft_table(draft: Any) -> Table:
    members = (draft or {}).get("members") or []
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("member", style="cli.key", no_wrap=True)
    table.add_column("role", no_wrap=True)
    table.add_column("mode", no_wrap=True)
    table.add_column("enabled", no_wrap=True)
    table.add_column("route / pool")
    for m in members:
        if not isinstance(m, dict):
            continue
        role = str((m.get("metadata") or {}).get("coding_role") or "")
        mode = str(m.get("model_mode") or "single")
        if mode == "multi":
            target = ", ".join(str(r) for r in (m.get("model_pool") or []))
        else:
            target = str(m.get("gateway_route_id") or "")
        table.add_row(
            str(m.get("id") or ""),
            Text(role, style=role_style(role)),
            mode,
            "yes" if m.get("enabled", True) else "no",
            truncate(target, 48),
        )
    return table


def render_draft(draft: Any) -> str:
    members = (draft or {}).get("members") or []
    room_id = (draft or {}).get("room_id")
    if room_id and not members:
        return render(
            heading("Team draft"),
            Text(f"backed by Council room: {room_id}", style="cli.key"),
            muted("apply it: errorta team apply --yes"),
        )
    if not members:
        return render(muted("(empty team draft — team set <role> <route> to start)"))
    return render(
        heading("Team draft"),
        _draft_table(draft),
        muted("apply it: errorta team apply --yes   (or: errorta run --members ...)"),
    )


def render_show(payload: Any) -> str:
    if (payload or {}).get("source") == "draft":
        return render_draft((payload or {}).get("draft"))
    # Projection fallback — derived + lossy (no coding_role, no enabled flag).
    return render(
        muted("(no local draft — showing the derived model-usage projection; it "
              "omits coding_role/enabled. Assemble an editable team with team set ...)"),
        render_projection((payload or {}).get("usage")),
    )


def render_projection(usage_payload: Any) -> str:
    usage = (usage_payload or {}).get("usage") or {}
    multi = usage.get("multi_members") or []
    single = usage.get("single_members") or []
    if not multi and not single:
        return render(muted("(no team configured — set one via team set / wizard / run setup)"))
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("member", style="cli.key", no_wrap=True)
    table.add_column("role", no_wrap=True)
    table.add_column("mode", no_wrap=True)
    table.add_column("route / pool")
    for m in single:
        table.add_row(str(m.get("member_id") or ""), Text("", style="cli.muted"),
                      "single", truncate(m.get("route_id"), 48))
    for m in multi:
        pool = m.get("pool") or []
        table.add_row(
            str(m.get("member_id") or ""),
            Text(str(m.get("role") or ""), style=role_style(m.get("role"))),
            "multi",
            truncate(", ".join(str(r) for r in pool), 48),
        )
    return render(heading("Team (model-usage projection)"), table)


def render_rooms(rooms_payload: Any) -> str:
    rooms = (rooms_payload or {}).get("rooms") or []
    if not rooms:
        return render(muted("(no Council rooms — create one in the app, or use team set ...)"))
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("room id", style="cli.key", no_wrap=True)
    table.add_column("name")
    for r in rooms:
        if isinstance(r, dict):
            table.add_row(str(r.get("id") or r.get("room_id") or ""),
                          truncate(r.get("name") or r.get("display_name"), 40))
    return render(
        heading("Council rooms"),
        table,
        muted("back the team with one: errorta team room <id>"),
    )


# Back-compat: the S2 name kept as an alias of the projection renderer.
render_team = render_show
