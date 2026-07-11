"""Preflight auto-warms the shared observed-connectivity cache.

When the member-health preflight probes a CLI/subscription provider and it comes
back ``connected``, that observation is recorded in the shared
``errorta_model_gateway.connectivity`` cache so the app's ``connect status`` /
``/gateway/providers`` can show ``connected`` without a manual Test. Crucially,
this is POSITIVE-only: a logged-out or errored probe records NOTHING, so the
cache can never assert a provider is reachable when it isn't.
"""
from __future__ import annotations

import asyncio

import pytest

from errorta_council.coding import member_health
from errorta_model_gateway import connectivity


def _run(coro):
    return asyncio.run(coro)


class _Handler:
    def __init__(self, state: str) -> None:
        self._state = state

    async def probe_auth(self) -> dict[str, str]:
        return {"state": self._state, "detail": ""}


class _BoomHandler:
    async def probe_auth(self) -> dict[str, str]:
        raise RuntimeError("probe blew up")


def _patch(monkeypatch, handler) -> None:
    from errorta_model_gateway.providers import async_registry

    monkeypatch.setattr(async_registry, "get_handler", lambda pc: handler)
    monkeypatch.setattr("errorta_model_gateway.loop_bridge.run_coro", _run)


@pytest.fixture(autouse=True)
def _clean_cache():
    connectivity.clear("claude_cli")
    yield
    connectivity.clear("claude_cli")


def test_connected_probe_warms_shared_cache(monkeypatch):
    _patch(monkeypatch, _Handler("connected"))
    failure = member_health._probe_route_status("claude_cli")
    assert failure.status == member_health.OK
    # observed connected ⇒ recorded (a timestamp is present)
    assert connectivity.observed_at("claude_cli") is not None


def test_logged_out_probe_records_nothing(monkeypatch):
    _patch(monkeypatch, _Handler("logged_out"))
    failure = member_health._probe_route_status("claude_cli")
    assert failure.status == member_health.AUTH_FAILED
    # a non-connected probe must NEVER warm the cache (no false positive)
    assert connectivity.observed_at("claude_cli") is None


def test_probe_error_records_nothing(monkeypatch):
    _patch(monkeypatch, _BoomHandler())
    failure = member_health._probe_route_status("claude_cli")
    assert failure.status != member_health.OK
    assert connectivity.observed_at("claude_cli") is None
