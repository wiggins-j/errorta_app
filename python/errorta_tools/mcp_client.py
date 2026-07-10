"""F045 — resilient MCP client wrapper.

Wraps an MCP transport with a timeout, a per-server circuit breaker, and
reconnect-on-failure. The transport is **injected** (an async callable) so this
slice needs no hard dependency on the `mcp` SDK and is fully testable with a
fake server. A real stdio / streamable-HTTP transport plugs in later behind
the same `Transport` protocol.

MCP tool results return as normalized ``ToolCallResult`` so they go through the
exact same request/result hash validation as every other ToolGateway result.
Elicitation requests are surfaced (not auto-answered) so the caller can route
them through F041 (see ``elicitation.py``).

Council never imports this module — it lives under ``errorta_tools`` and only
ToolGateway protocol/value types cross the seam.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from .gateway import (
    FatalToolError,
    RetryableToolError,
    ToolCallRequest,
    ToolCallResult,
)
from .mcp_state import REASON_CIRCUIT_OPEN, McpServerHealth


class McpElicitationRequired(Exception):
    """Raised when an MCP call needs user input. Carries safe metadata only —
    the caller routes it through F041 (never auto-answers)."""

    def __init__(self, *, prompt_summary: str, schema: dict[str, Any] | None = None) -> None:
        super().__init__("mcp_elicitation_required")
        self.prompt_summary = prompt_summary
        self.schema = schema or {}


@dataclass(frozen=True)
class McpToolOutput:
    """What an MCP transport returns for a tool call."""

    content: str
    egress_class: str = "remote"
    is_error: bool = False
    error_reason: str | None = None


class Transport(Protocol):
    async def call_tool(
        self, *, server_id: str, tool_id: str, arguments: dict[str, Any]
    ) -> McpToolOutput: ...

    async def list_tools(self, *, server_id: str) -> list[str]: ...


class McpClient:
    """Circuit-broken, timeout-bounded MCP client over an injected transport."""

    def __init__(self, *, transport: Transport) -> None:
        self._transport = transport

    async def health_probe(self, health: McpServerHealth) -> McpServerHealth:
        """Refresh reachability + tool count WITHOUT invoking any tool.

        Respects the breaker: when open (and cooling down) it does not probe.
        A probe failure feeds the breaker like any other failure.
        """
        if not health.breaker.allow():
            health.reachable = False
            return health
        try:
            tools = await self._transport.list_tools(server_id=health.server_id)
            health.reachable = True
            health.tool_count = len(tools)
            health.breaker.record_success()
        except Exception as exc:  # noqa: BLE001 - normalized below
            health.reachable = False
            health.breaker.record_failure(reason=_classify(exc))
        return health

    async def call_tool(
        self,
        *,
        request: ToolCallRequest,
        health: McpServerHealth,
        timeout_seconds: float,
    ) -> ToolCallResult:
        """Invoke an MCP tool, fail-closed if the circuit is open.

        Raises:
        - ``FatalToolError`` when the circuit is open (stable reason code).
        - ``McpElicitationRequired`` when the server asks for user input.
        - ``RetryableToolError`` / ``FatalToolError`` on transport failures
          (also recorded on the breaker).
        """
        if not health.breaker.allow():
            raise FatalToolError(
                f"{REASON_CIRCUIT_OPEN}: server {health.server_id!r} circuit is open"
            )
        loop_start = _monotonic()
        try:
            output = await asyncio.wait_for(
                self._transport.call_tool(
                    server_id=health.server_id,
                    tool_id=request.tool_id,
                    arguments=dict(request.arguments),
                ),
                timeout=timeout_seconds,
            )
        except McpElicitationRequired:
            # Not a failure — do not trip the breaker; surface for F041.
            raise
        except asyncio.TimeoutError:
            health.breaker.record_failure(reason="mcp_timeout")
            raise RetryableToolError(f"mcp_timeout: server {health.server_id!r}") from None
        except Exception as exc:  # noqa: BLE001
            reason = _classify(exc)
            health.breaker.record_failure(reason=reason)
            raise RetryableToolError(f"{reason}: server {health.server_id!r}") from None

        if output.is_error:
            health.breaker.record_failure(reason=output.error_reason or "mcp_tool_error")
            raise FatalToolError(
                f"mcp_tool_error: {(output.error_reason or 'unknown')[:120]}"
            )

        health.breaker.record_success()
        duration_ms = int((_monotonic() - loop_start) * 1000)
        return ToolCallResult.from_content(
            request=request,
            content=output.content,
            duration_ms=duration_ms,
            egress_class=output.egress_class,
            provenance={"backend": "mcp", "server_id": health.server_id},
        )


def _classify(exc: Exception) -> str:
    """Map a transport exception to a stable reason code — never raw text."""
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "mcp_timeout"
    if "connection" in name or "connect" in name:
        return "mcp_connection_error"
    return "mcp_transport_error"


def _monotonic() -> float:
    import time

    return time.monotonic()
