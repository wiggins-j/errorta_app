"""Repo-grounded next-goal proposal + the shared run-start gate.

Two things live here, both consumed by the Slack surface (``errorta_slack``)
and neither importing it:

1. :func:`start_gate` — the single implementation of "may this project start a
   run?". Called by ``errorta_slack.tools.start_run`` AND
   ``errorta_slack.studio_tools.adopt_project``. One implementation is the
   point: two copies of a gate is how one of them ends up missing.
2. :func:`propose_next_goal` — a bounded read of the project's real repo +
   docs + commits, turned into a PROPOSED next goal by one model call. It
   writes nothing; only a human-confirmed ``set_next_goal`` writes.

Deliberately plain ``errorta_council.coding`` library code: no import from
``errorta_app.routes.*`` or ``errorta_slack``, and heavy imports are done
inside functions to keep the module cheap to import.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

MemberCaller = Callable[[dict[str, Any], str], str]

_NO_GOAL_REFUSAL = (
    "no current goal — the team would plan against the North Star alone, "
    "which may be stale. Set the next goal first (I can read the repo and "
    "propose one)."
)


def start_gate(store: Any) -> str | None:
    """Return a refusal reason, or ``None`` when the project may start.

    Refuses when there is no active Focus AND no legacy ``work_request``.
    Rationale (spec §3.4): ``runner._pm_prompt`` scopes planning by
    ``active_focuses()`` and falls back to ``work_request``; with neither, the
    PM plans from the North Star alone. On a project whose charter has gone
    stale that spends real model budget re-litigating finished work.

    **Fails OPEN.** A ledger this function cannot read returns ``None``
    (allow), never a raise and never a refusal — a read error must not wedge
    every project behind a gate, and this runs on every start path including
    autopilot's.
    """
    try:
        if store.active_focuses():
            return None
    except Exception:  # noqa: BLE001 - unreadable focus ledger -> fail open
        return None
    try:
        if str(store.get_project().work_request or "").strip():
            return None
    except Exception:  # noqa: BLE001 - unreadable project -> fail open
        return None
    return _NO_GOAL_REFUSAL
