"""``prs`` (list) + ``pr <id>`` (detail + diff).

``prs`` → ``GET /prs``. ``pr <id>`` matches the PR then fetches the worktree diff
(``GET /worktree``) and renders it through ``delta``/pager when present.
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, Param, register
from ..render.prs import render_pr_detail, render_prs
from ..session import Context
from . import _base


def _call_list(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    return client.get_json(f"/coding/projects/{ctx.project_id}/prs")


def _call_detail(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    pr_id = args.get("id")
    if not pr_id:
        return _base.usage("pr <id>")
    prs = (client.get_json(f"/coding/projects/{ctx.project_id}/prs") or {}).get("prs") or []
    matched = next((p for p in prs if str(p.get("pr_id")) == str(pr_id)), None)
    # The worktree preview is the whole delivered diff (there is no per-PR diff
    # route); it's the closest thing to "the PR's diff" the engine exposes.
    worktree = client.get_json(f"/coding/projects/{ctx.project_id}/worktree") or {}
    return {
        "pr": matched,
        "gate": worktree.get("gate"),
        "diff": worktree.get("diff"),
    }


register(
    Command(
        name="prs",
        help="Pull requests (branch-per-task review/test/merge state).",
        call=_call_list,
        render=_base.make_render(render_prs),
        params=(Param("watch", "re-render on the poll loop", is_flag=True),),
    )
)

register(
    Command(
        name="pr",
        help="One PR's detail + worktree diff (via delta/pager if present).",
        call=_call_detail,
        render=_base.make_render(render_pr_detail),
        params=(Param("id", "pr id", required=True),),
    )
)
