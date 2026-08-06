"""SPEC-41 Moves 1-3 — truncation honesty, `think:false`, coupled `format`.

Move 1: `gateway_local` substituted `THINKING_TRACE_MARKER + thinking` whenever
`content` was empty. A TRUNCATION (`done_reason == "length"`) therefore reached the
council as a partial reasoning trace *presented as an answer* — which is how it
surfaces downstream as F001's "wrong-schema JSON". The marker is now kept only for
its actual purpose: a model that finished normally having emitted reasoning only.

Moves 2+3: `think: false` on structured local turns (measured 4/6 -> 6/6 schema
compliance and 2197 -> 33 generated tokens), with `format: "json"` **gated on it** —
issue #84's warning is that constraining the output channel while the thinking
channel is live is the pairing that empties `content`.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from errorta_council.gateway_local import (
    THINKING_TRACE_MARKER,
    LocalCouncilModelRequest,
    LocalGateway,
)


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """Captures the posted body and returns a scripted Ollama payload."""

    def __init__(self, payload: dict[str, Any], sink: dict[str, Any]) -> None:
        self._payload = payload
        self._sink = sink

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
        self._sink.clear()
        self._sink.update(json)
        return _FakeResponse(self._payload)


def _dispatch(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any],
              *, metadata: dict[str, Any] | None = None) -> tuple[Any, dict[str, Any]]:
    """Run `_ollama_dispatch` against a scripted payload; return (result, sent body)."""
    sent: dict[str, Any] = {}
    import errorta_council.gateway_local as gl

    monkeypatch.setattr(
        gl.httpx, "AsyncClient",
        lambda **kw: _FakeClient(payload, sent))
    req = LocalCouncilModelRequest(
        role="reviewer", route_id="local.qwen3.5:9b", provider="local",
        model="qwen3.5:9b", messages=[{"role": "user", "content": "hi"}],
        max_output_tokens=8192, temperature=0.0, timeout_seconds=600,
        metadata=metadata or {})
    result = asyncio.run(LocalGateway()._ollama_dispatch(req))
    return result, sent


# --------------------------------------------------------------------------- #
# Move 1 — a truncation is not an answer
# --------------------------------------------------------------------------- #
def test_truncation_does_not_get_the_thinking_marker(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """THE REGRESSION. Before this, a clipped reasoning trace was handed to the
    council prefixed as though it were a deliberate thinking-only answer."""
    result, _ = _dispatch(monkeypatch, {
        "message": {"content": "", "thinking": "I should check whether add() ..."},
        "done_reason": "length"})
    assert result.truncated is True
    assert result.content == ""
    assert not result.content.startswith(THINKING_TRACE_MARKER)
    assert result.is_thinking_burn is False


def test_genuine_thinking_burn_keeps_the_marker(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The marker's real purpose is preserved: finished normally, reasoning only."""
    result, _ = _dispatch(monkeypatch, {
        "message": {"content": "", "thinking": "pondering"},
        "done_reason": "stop"})
    assert result.truncated is False
    assert result.content.startswith(THINKING_TRACE_MARKER)
    assert result.is_thinking_burn is True


def test_a_clipped_but_non_empty_answer_is_flagged(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Content survives, but the caller can tell it is incomplete."""
    result, _ = _dispatch(monkeypatch, {
        "message": {"content": '{"approved": fal'},
        "done_reason": "length"})
    assert result.truncated is True
    assert result.content == '{"approved": fal'


def test_a_normal_answer_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = _dispatch(monkeypatch, {
        "message": {"content": '{"approved": true}'}, "done_reason": "stop"})
    assert result.truncated is False


# --------------------------------------------------------------------------- #
# Moves 2+3 — think:false, and format COUPLED to it
# --------------------------------------------------------------------------- #
def test_unstructured_turn_sends_neither_field(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """No `structured_output` -> today's request byte-for-byte."""
    _, sent = _dispatch(monkeypatch, {"message": {"content": "ok"}})
    assert "think" not in sent
    assert "format" not in sent


def test_structured_turn_sends_think_false_and_format(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _, sent = _dispatch(monkeypatch, {"message": {"content": "ok"}},
                        metadata={"structured_output": True})
    assert sent["think"] is False
    assert sent["format"] == "json"


def test_format_is_unreachable_while_thinking_is_live(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """THE #84 COUPLING. An earlier draft made these independent knobs, which made
    the harmful pairing a *supported* configuration. It must be unreachable."""
    _, sent = _dispatch(monkeypatch, {"message": {"content": "ok"}},
                        metadata={"structured_output": True,
                                  "local_think_false": False,
                                  "local_structured_format": True})
    assert "think" not in sent, "thinking must be left live when the knob is off"
    assert "format" not in sent, (
        "format must NOT be sent while the thinking channel is live — that is the "
        "combination issue #84 warns about")


def test_format_can_be_disabled_without_disabling_think_false(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The coupling is one-directional: think:false alone is a valid config."""
    _, sent = _dispatch(monkeypatch, {"message": {"content": "ok"}},
                        metadata={"structured_output": True,
                                  "local_structured_format": False})
    assert sent["think"] is False
    assert "format" not in sent


def test_policy_knobs_round_trip() -> None:
    from errorta_council.coding.autonomy import (
        CodingAutonomyPolicy,
        policy_from_dict,
        policy_to_dict,
    )
    p = CodingAutonomyPolicy()
    assert p.local_think_false is True
    assert p.local_structured_format is True
    d = policy_to_dict(p)
    assert d["local_think_false"] is True and d["local_structured_format"] is True
    off = policy_from_dict({**d, "local_think_false": False})
    assert off.local_think_false is False
    assert off.local_structured_format is True   # independent field...
    # ...but the gateway still refuses to send `format` — asserted above.
