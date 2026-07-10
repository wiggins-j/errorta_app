"""F045 — MCP circuit breaker, client resilience, and elicitation -> F041."""
from __future__ import annotations

import asyncio

import pytest

from errorta_tools.gateway import FatalToolError, RetryableToolError, ToolCallRequest
from errorta_tools.mcp_client import McpClient, McpElicitationRequired, McpToolOutput
from errorta_tools.mcp_state import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    REASON_CIRCUIT_OPEN,
    CircuitBreaker,
    McpStateRegistry,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# --- circuit breaker state machine -----------------------------------------

def test_breaker_opens_after_threshold():
    cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=30, _now=lambda: 0)
    assert cb.state == CLOSED and cb.allow()
    for _ in range(2):
        cb.record_failure(reason="x")
    assert cb.state == CLOSED  # 2 < 3
    cb.record_failure(reason="x")
    assert cb.state == OPEN
    assert cb.allow() is False  # fail closed while open


def test_breaker_cooldown_moves_to_half_open_then_closes_on_success():
    clk = _Clock()
    cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=30, _now=clk)
    cb.record_failure(reason="x")
    cb.record_failure(reason="x")
    assert cb.state == OPEN and cb.allow() is False
    clk.t = 31  # cooldown elapsed
    assert cb.allow() is True  # probe permitted
    assert cb.state == HALF_OPEN
    cb.record_success()
    assert cb.state == CLOSED and cb.consecutive_failures == 0


def test_breaker_reopens_when_probe_fails():
    clk = _Clock()
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=10, _now=clk)
    cb.record_failure(reason="x")
    assert cb.state == OPEN
    clk.t = 11
    assert cb.allow() and cb.state == HALF_OPEN
    cb.record_failure(reason="x")  # probe fails
    assert cb.state == OPEN
    assert cb.opened_at == 11  # cooldown clock reset


# --- client over a fake transport ------------------------------------------

class _FakeTransport:
    def __init__(self, *, behavior):
        self.behavior = behavior
        self.calls = 0

    async def call_tool(self, *, server_id, tool_id, arguments):
        self.calls += 1
        b = self.behavior
        if b == "ok":
            return McpToolOutput(content="MCP RESULT", egress_class="remote")
        if b == "error":
            return McpToolOutput(content="", is_error=True, error_reason="mcp_tool_error")
        if b == "elicit":
            raise McpElicitationRequired(prompt_summary="need a token", schema={"k": 1})
        if b == "boom":
            raise ConnectionError("dead")
        if b == "hang":
            await asyncio.sleep(60)
        raise AssertionError(b)

    async def list_tools(self, *, server_id):
        if self.behavior == "boom":
            raise ConnectionError("dead")
        return ["t1", "t2"]


def _req():
    return ToolCallRequest(
        call_id="c1", run_id="run-1", turn_id="t-1", member_id="m-1",
        tool_id="mcp.search", arguments={"q": "x"},
    )


@pytest.mark.asyncio
async def test_client_success_returns_normalized_result():
    reg = McpStateRegistry()
    health = reg.ensure("srv", enabled=True)
    client = McpClient(transport=_FakeTransport(behavior="ok"))
    result = await client.call_tool(request=_req(), health=health, timeout_seconds=5)
    assert result.content == "MCP RESULT"
    assert result.content_sha256  # hash-validated like every ToolGateway result
    assert result.provenance["backend"] == "mcp"
    assert health.breaker.state == CLOSED


@pytest.mark.asyncio
async def test_client_circuit_open_fails_closed_with_reason_code():
    reg = McpStateRegistry()
    health = reg.ensure("srv", enabled=True, failure_threshold=1)
    health.breaker.record_failure(reason="x")  # trip it open
    client = McpClient(transport=_FakeTransport(behavior="ok"))
    with pytest.raises(FatalToolError) as e:
        await client.call_tool(request=_req(), health=health, timeout_seconds=5)
    assert REASON_CIRCUIT_OPEN in str(e.value)


@pytest.mark.asyncio
async def test_client_transport_failure_trips_breaker():
    reg = McpStateRegistry()
    health = reg.ensure("srv", enabled=True, failure_threshold=1)
    client = McpClient(transport=_FakeTransport(behavior="boom"))
    with pytest.raises(RetryableToolError):
        await client.call_tool(request=_req(), health=health, timeout_seconds=5)
    assert health.breaker.state == OPEN
    assert health.breaker.last_failure_reason == "mcp_connection_error"


@pytest.mark.asyncio
async def test_client_timeout_is_retryable_and_recorded():
    reg = McpStateRegistry()
    health = reg.ensure("srv", enabled=True, failure_threshold=1)
    client = McpClient(transport=_FakeTransport(behavior="hang"))
    with pytest.raises(RetryableToolError):
        await client.call_tool(request=_req(), health=health, timeout_seconds=0.05)
    assert health.breaker.last_failure_reason == "mcp_timeout"


@pytest.mark.asyncio
async def test_elicitation_surfaces_and_does_not_trip_breaker():
    reg = McpStateRegistry()
    health = reg.ensure("srv", enabled=True, failure_threshold=1)
    client = McpClient(transport=_FakeTransport(behavior="elicit"))
    with pytest.raises(McpElicitationRequired) as e:
        await client.call_tool(request=_req(), health=health, timeout_seconds=5)
    assert e.value.prompt_summary == "need a token"
    assert health.breaker.state == CLOSED  # elicitation is not a failure


@pytest.mark.asyncio
async def test_health_probe_does_not_invoke_tools():
    reg = McpStateRegistry()
    health = reg.ensure("srv", enabled=True)
    transport = _FakeTransport(behavior="ok")
    client = McpClient(transport=transport)
    await client.health_probe(health)
    assert health.reachable is True and health.tool_count == 2
    assert transport.calls == 0  # list_tools only, never call_tool


def test_elicitation_builds_f041_pending_request():
    from errorta_tools.elicitation import build_elicitation_pending_request
    from errorta_policy import PolicyPhase

    req = build_elicitation_pending_request(
        run_id="run-1", server_id="srv", tool_id="mcp.x", member_id="m-1",
        prompt_summary="grant access?", schema={"type": "object"},
    )
    assert req.phase == PolicyPhase.MCP_ELICITATION
    assert req.reason_code == "mcp_elicitation_required"
    # Safe metadata only — no raw schema embedded, just a hash.
    assert "schema_sha256" in req.safe_request
    assert "type" not in str(req.safe_request)


def test_council_does_not_import_mcp_clients():
    """No-egress invariant: errorta_council must not import MCP client modules."""
    import ast
    from pathlib import Path

    council_dir = Path(__file__).parents[2] / "errorta_council"
    forbidden = {"mcp", "mcp_client"}
    violations: list[str] = []
    for path in council_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for m in mods:
                parts = set(m.split("."))
                if parts & forbidden or m.endswith("mcp_client"):
                    violations.append(f"{path.name}: {m}")
    assert violations == [], violations
