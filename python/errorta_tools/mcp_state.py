"""F045 — per-MCP-server health + circuit breaker state.

Repeated MCP failures must not wedge a Council run. Each server has a circuit
breaker (closed -> open -> half-open -> closed) with a failure threshold and a
cooldown. When the circuit is open, calls fail closed with a stable reason code
instead of hanging on a dead server. Pure in-memory state machine — no I/O —
so it's deterministic and fully testable.

Time is injected (``now`` callable) so cooldown is testable without sleeps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# Circuit states.
CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

REASON_CIRCUIT_OPEN = "mcp_circuit_open"


@dataclass
class CircuitBreaker:
    """A single server's circuit breaker.

    - ``failure_threshold`` consecutive failures trip the circuit OPEN.
    - After ``cooldown_seconds`` the next ``allow()`` moves it to HALF_OPEN and
      permits a single probe.
    - A success in HALF_OPEN closes the circuit; a failure re-opens it.
    """

    failure_threshold: int = 3
    cooldown_seconds: float = 30.0
    state: str = CLOSED
    consecutive_failures: int = 0
    opened_at: float | None = None
    last_failure_reason: str | None = None
    _now: Callable[[], float] = field(default=lambda: 0.0, repr=False)

    def allow(self) -> bool:
        """Whether a call may proceed now (advances OPEN -> HALF_OPEN on cooldown)."""
        if self.state == CLOSED:
            return True
        if self.state == HALF_OPEN:
            # A probe is already in flight conceptually; permit it.
            return True
        # OPEN: permit a single probe once the cooldown elapsed.
        if self.opened_at is not None and self._now() - self.opened_at >= self.cooldown_seconds:
            self.state = HALF_OPEN
            return True
        return False

    def record_success(self) -> None:
        self.state = CLOSED
        self.consecutive_failures = 0
        self.opened_at = None
        self.last_failure_reason = None

    def record_failure(self, *, reason: str) -> None:
        self.last_failure_reason = reason
        if self.state == HALF_OPEN:
            # Probe failed — straight back to OPEN, reset the cooldown clock.
            self.state = OPEN
            self.opened_at = self._now()
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self.state = OPEN
            self.opened_at = self._now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "failure_threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown_seconds,
            "last_failure_reason": self.last_failure_reason,
        }


@dataclass
class McpServerHealth:
    server_id: str
    configured: bool
    enabled: bool
    breaker: CircuitBreaker
    reachable: bool | None = None  # None = not probed yet
    tool_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "configured": self.configured,
            "enabled": self.enabled,
            "reachable": self.reachable,
            "tool_count": self.tool_count,
            "circuit": self.breaker.to_dict(),
        }


class McpStateRegistry:
    """In-process registry of per-server health + breakers."""

    def __init__(self, *, now: Callable[[], float] | None = None) -> None:
        self._now = now or (lambda: 0.0)
        self._servers: dict[str, McpServerHealth] = {}

    def ensure(
        self,
        server_id: str,
        *,
        configured: bool = True,
        enabled: bool = False,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
    ) -> McpServerHealth:
        existing = self._servers.get(server_id)
        if existing is not None:
            return existing
        health = McpServerHealth(
            server_id=server_id,
            configured=configured,
            enabled=enabled,
            breaker=CircuitBreaker(
                failure_threshold=failure_threshold,
                cooldown_seconds=cooldown_seconds,
                _now=self._now,
            ),
        )
        self._servers[server_id] = health
        return health

    def get(self, server_id: str) -> McpServerHealth | None:
        return self._servers.get(server_id)

    def all(self) -> list[McpServerHealth]:
        return [self._servers[k] for k in sorted(self._servers)]
