"""F045 — convert an MCP elicitation request into an F041 pending decision.

An MCP server can ask the user for input/permission mid-call ("elicitation").
That must never auto-proceed: it routes through F041's pending-decision
mechanism exactly like tool first-use consent, so the run pauses in
``awaiting_user_decision`` and applies nothing until the user approves.

This module builds the F041 ``PendingDecisionRequest`` from safe metadata only
(no raw server payload). The caller (ToolGateway/scheduler) persists it through
``errorta_policy.PendingDecisionStore``.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _sha(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_elicitation_pending_request(
    *,
    run_id: str,
    server_id: str,
    tool_id: str,
    member_id: str | None,
    prompt_summary: str,
    schema: dict[str, Any] | None = None,
):
    """Return an ``errorta_policy.PendingDecisionRequest`` for an MCP elicitation.

    ``prompt_summary`` is a short, safe description of what the server is asking
    for. The raw elicitation schema is hashed, never embedded.
    """
    # Imported here so this module has no hard dependency on errorta_policy at
    # import time (keeps the no-egress import graph clean for callers).
    from errorta_policy import PendingDecisionRequest, PolicyPhase

    return PendingDecisionRequest(
        run_id=run_id,
        phase=PolicyPhase.MCP_ELICITATION,
        reason_code="mcp_elicitation_required",
        requester={
            "type": "mcp_server",
            "server_id": server_id,
            "tool_id": tool_id,
            "member_id": member_id,
        },
        safe_request={
            "server_id": server_id,
            "tool_id": tool_id,
            "prompt_summary": str(prompt_summary)[:500],
            "schema_sha256": _sha(schema or {}),
        },
        risk_class="mcp_elicitation",
        created_by_policy_id="errorta_tools_mcp",
        metadata={"backend": "mcp"},
    )
