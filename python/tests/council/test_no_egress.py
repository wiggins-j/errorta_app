"""F031 Phase 1 marquee acceptance gate (invariants 3, 10)."""
from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from errorta_app.server import app


_OLLAMA_HOSTS = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture
def hard_monkeypatch_network(monkeypatch):
    """Replace all common HTTP/socket egress with bombs that allow only Ollama loopback."""
    real_socket = socket.socket

    class _BombSocket(real_socket):  # type: ignore[misc, valid-type]
        def connect(self, address):  # type: ignore[override]
            host = address[0] if isinstance(address, tuple) else str(address)
            if host not in _OLLAMA_HOSTS:
                raise AssertionError(f"non-Ollama egress attempted: {host}")
            return super().connect(address)

    monkeypatch.setattr(socket, "socket", _BombSocket)

    import urllib.request
    def _bomb_urlopen(*a, **k):
        raise AssertionError("urllib egress not allowed in Council")
    monkeypatch.setattr(urllib.request, "urlopen", _bomb_urlopen)

    try:
        import aiohttp

        class _BombSession:
            def __init__(self, *a, **k) -> None:
                raise AssertionError("aiohttp egress not allowed in Council")
        monkeypatch.setattr(aiohttp, "ClientSession", _BombSession)
    except ImportError:
        pass


def _await_terminal(client: TestClient, run_id: str, *, max_polls: int = 200) -> dict:
    for _ in range(max_polls):
        meta = client.get(f"/council/runs/{run_id}").json()["run"]
        if meta["status"] in ("completed", "failed", "cancelled"):
            return meta
    raise AssertionError(f"run {run_id} did not reach terminal")


def test_full_mvp_path_fake_provider_makes_zero_http(
    tmp_errorta_home, hard_monkeypatch_network, seed_room_full
) -> None:
    """Marquee: fake provider + full pause/resume permutation, zero HTTP."""
    import httpx

    class _BombAsyncClient:
        def __init__(self, *a, **k) -> None:
            raise AssertionError("httpx egress not allowed for fake provider")

    # Monkeypatch httpx.AsyncClient at the module level for the duration of test.
    original = httpx.AsyncClient
    httpx.AsyncClient = _BombAsyncClient  # type: ignore[assignment]
    try:
        room = seed_room_full(
            room_id="rm-no-egress-fake",
            member_count=2,
            provider="fake",
            model="stub-model",
            max_rounds=2,
            max_messages_per_member=1,
        )
        client = TestClient(app)
        r = client.post(
            "/council/runs",
            json={"room_id": room.id, "prompt": "hi", "corpus_ids": []},
        )
        run_id = r.json()["run"]["id"]
        client.post(f"/council/runs/{run_id}/pause")
        client.post(f"/council/runs/{run_id}/resume")
        meta = _await_terminal(client, run_id)
        assert meta["status"] == "completed"
    finally:
        httpx.AsyncClient = original  # type: ignore[assignment]


def test_full_mvp_path_local_provider_only_hits_ollama(
    tmp_errorta_home, hard_monkeypatch_network, monkeypatch, seed_room_full
) -> None:
    """Local provider may only reach 127.0.0.1; any other egress fails."""
    from errorta_council import gateway_local
    from errorta_council.gateway_local import LocalCouncilModelResult

    async def _stub_dispatch(self, request):
        return LocalCouncilModelResult(
            content="stub",
            provider="ollama",
            provider_class="local",
            model=request.model,
            input_tokens=5,
            output_tokens=3,
            duration_ms=1,
            raw_usage_available=True,
        )

    async def _reachable(self): return True
    async def _installed(self): return ["stub-model"]

    monkeypatch.setattr(gateway_local.LocalGateway, "_ollama_dispatch", _stub_dispatch)
    monkeypatch.setattr(gateway_local.LocalGateway, "is_reachable", _reachable)
    monkeypatch.setattr(gateway_local.LocalGateway, "list_installed_models", _installed)

    room = seed_room_full(
        room_id="rm-no-egress-local",
        member_count=2,
        provider="local",
        model="stub-model",
        max_rounds=1,
        max_messages_per_member=1,
    )
    client = TestClient(app)
    r = client.post(
        "/council/runs",
        json={"room_id": room.id, "prompt": "hi", "corpus_ids": []},
    )
    run_id = r.json()["run"]["id"]
    meta = _await_terminal(client, run_id)
    assert meta["status"] == "completed"
