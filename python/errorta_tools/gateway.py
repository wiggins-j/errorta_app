"""ToolGateway Protocol and normalized request/result value types.

Slice 1 deliberately ships no real tools. The default gateway dispatches to
registered handlers so tests and future MCP-backed tools can prove the seam
without letting ``errorta_council`` import any egress-capable module.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Protocol


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stable_json_sha256(value: Any) -> str:
    """Hash JSON-like data deterministically for audit/provenance."""
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode()).hexdigest()


class ToolGatewayError(Exception):
    """Base class for ToolGateway failures."""


class RetryableToolError(ToolGatewayError):
    """Transient tool failure. A future retry policy may recover."""


class FatalToolError(ToolGatewayError):
    """Non-recoverable tool failure or policy violation."""


@dataclass(frozen=True)
class ToolCallRequest:
    """Normalized tool invocation request.

    ``arguments`` are untrusted model-provided data. Callers should use
    ``args_sha256`` in events/logs and keep raw arguments out of broad UI
    projections unless an inspection policy explicitly grants them.
    """

    call_id: str
    run_id: str
    turn_id: str
    member_id: str
    tool_id: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    context_id: str | None = None
    requested_at: str = field(default_factory=_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def args_sha256(self) -> str:
        return stable_json_sha256(self.arguments)

    def without_raw_arguments(self) -> dict[str, Any]:
        """Event-safe projection; no raw arguments."""
        return {
            "call_id": self.call_id,
            "tool_id": self.tool_id,
            "args_sha256": self.args_sha256,
            "reason": self.reason,
            "context_id": self.context_id,
            "requested_at": self.requested_at,
        }


@dataclass(frozen=True)
class ToolCallResult:
    """Normalized tool result.

    Raw ``content`` is untrusted data. The scheduler persists it in the
    tool-result side store and emits only the audit projection into the run
    event log.
    """

    call_id: str
    tool_id: str
    content: str
    content_sha256: str
    produced_at: str
    duration_ms: int
    egress_class: str = "local"
    status: str = "completed"
    cost_usd: float | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_content(
        cls,
        *,
        request: ToolCallRequest,
        content: str,
        duration_ms: int,
        egress_class: str = "local",
        status: str = "completed",
        cost_usd: float | None = None,
        provenance: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ToolCallResult":
        base_provenance = {
            "tool_id": request.tool_id,
            "args_sha256": request.args_sha256,
            "requested_at": request.requested_at,
        }
        if provenance:
            base_provenance.update(provenance)
        return cls(
            call_id=request.call_id,
            tool_id=request.tool_id,
            content=content,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            produced_at=_now_iso(),
            duration_ms=int(duration_ms),
            egress_class=egress_class,
            status=status,
            cost_usd=cost_usd,
            provenance=base_provenance,
            metadata=dict(metadata or {}),
        )

    def with_content(self, content: str) -> "ToolCallResult":
        """Return a copy with recomputed content hash."""
        return replace(
            self,
            content=content,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        )

    def audit_projection(self) -> dict[str, Any]:
        """Event/inspection-safe projection; excludes raw content."""
        safe_provenance = {
            k: self.provenance[k]
            for k in ("tool_id", "args_sha256", "requested_at")
            if k in self.provenance
        }
        safe_metadata = {
            "keys": sorted(str(k) for k in self.metadata.keys()),
            "sha256": stable_json_sha256(self.metadata) if self.metadata else None,
        }
        return {
            "call_id": self.call_id,
            "tool_id": self.tool_id,
            "content_sha256": self.content_sha256,
            "produced_at": self.produced_at,
            "duration_ms": self.duration_ms,
            "egress_class": self.egress_class,
            "status": self.status,
            "cost_usd": self.cost_usd,
            "provenance": safe_provenance,
            "metadata": safe_metadata,
        }


class ToolHandler(Protocol):
    tool_id: str

    async def invoke(self, request: ToolCallRequest) -> ToolCallResult:
        """Run a tool behind the gateway seam."""
        ...


class ToolGateway(Protocol):
    async def invoke(self, request: ToolCallRequest) -> ToolCallResult:
        """Invoke one normalized tool request."""
        ...


class DefaultToolGateway:
    """Registry-backed gateway used by tests and future concrete tools."""

    async def invoke(self, request: ToolCallRequest) -> ToolCallResult:
        from .registry import get_handler

        handler = get_handler(request.tool_id)
        if handler is None:
            raise FatalToolError(f"tool_not_registered: {request.tool_id}")
        return await handler.invoke(request)


__all__ = [
    "DefaultToolGateway",
    "FatalToolError",
    "RetryableToolError",
    "ToolCallRequest",
    "ToolCallResult",
    "ToolGateway",
    "ToolGatewayError",
    "ToolHandler",
    "stable_json_sha256",
]
