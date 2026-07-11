"""Shared helpers for the read commands (F147 S2).

Every read command follows the same skeleton:

* the ``call`` returns a sentinel ``{"_no_project": True}`` when no project is
  bound (so it never builds a ``.../None`` route, and both front-ends stay in
  parity — zero route calls on either surface); a ``{"_usage": ...}`` sentinel
  when a required positional argument is missing;
* the ``render`` short-circuits to ``--json`` (raw payload), then to the friendly
  no-project / usage lines, then delegates to the view renderer.

``make_render`` wires that skeleton so each command module only writes its route
call + its Rich view function.
"""
from __future__ import annotations

from typing import Any, Callable

from .. import render as _render
from ..registry import render_json
from ..session import Context
from ..verbosity import Verbosity

# A view renderer: payload + verbosity → string (already past --json/sentinels).
ViewFn = Callable[[Any, Verbosity], str]


def no_project() -> dict[str, Any]:
    return {"_no_project": True}


def usage(text: str) -> dict[str, Any]:
    return {"_usage": text}


def make_render(view: ViewFn) -> Callable[[Any, Verbosity, bool], str]:
    """Build a registry ``render`` from a view function.

    Order: ``--json`` (raw) → no-project → usage → the view. This is the ONLY
    place a renderer decides between raw and human output, so the no-secret-leak
    invariant is uniform across every command.
    """

    def _render_fn(payload: Any, verbosity: Verbosity, json_mode: bool) -> str:
        if json_mode:
            return render_json(payload)
        if _render.is_no_project(payload):
            return _render.no_project()
        usage_line = _render.usage_text(payload)
        if usage_line is not None:
            return _render.render(_render.muted(f"usage: {usage_line}"))
        return view(payload, verbosity)

    return _render_fn


def has_project(ctx: Context) -> bool:
    return bool(ctx.project_id)
