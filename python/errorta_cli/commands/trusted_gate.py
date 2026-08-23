"""``trusted-gate`` — the operator-declared trusted gate, read-only (spec
2026-08-23-trusted-gate).

``GET /coding/projects/{id}/trusted-gate`` -> ``{tier, path, present, valid,
code, commands:[{id, argv, cwd, timeout_seconds, scope}], env_passthrough}``.
There is no ``set``/``put`` here on purpose: the gate file under
``$ERRORTA_HOME/gates`` is operator-authored, never engine-written (see
``tests/test_gates_dir_is_operator_only.py``); this command only shows what an
operator already put there.

Mirrors ``gate.py``: a project-bound GET with a no-project guard and ``--json``
for scripting.
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, register
from ..render.trusted_gate import render_trusted_gate
from ..session import Context
from . import _base


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    return client.get_json(f"/coding/projects/{ctx.project_id}/trusted-gate")


register(
    Command(
        name="trusted-gate",
        help="Show the project's operator-declared trusted gate (read-only).",
        call=_call,
        render=_base.make_render(render_trusted_gate),
    )
)
