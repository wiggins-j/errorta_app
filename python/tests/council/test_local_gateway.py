from __future__ import annotations

import pytest

from errorta_council.gateway_local import (
    FatalError,
    LocalCouncilModelRequest,
    LocalGateway,
    RetryableError,
)


def _req(provider: str = "local", model: str = "llama3.2:1b") -> LocalCouncilModelRequest:
    return LocalCouncilModelRequest(
        role="member",
        route_id="r1",
        provider=provider,
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        max_output_tokens=64,
        temperature=0.0,
        timeout_seconds=5,
        metadata={"context_id": "ctx-x"},
    )


@pytest.mark.asyncio
async def test_unsupported_provider_class_raises_before_http(monkeypatch) -> None:
    """Anthropic/OpenAI/etc must be rejected before any HTTP attempt (invariant 3)."""
    import httpx
    called = {"n": 0}

    class _BombClient:
        def __init__(self, *args, **kwargs) -> None:
            called["n"] += 1
            raise AssertionError("HTTP client must not be initialized for non-local/non-fake")

    monkeypatch.setattr(httpx, "AsyncClient", _BombClient)
    gw = LocalGateway(base_url="http://127.0.0.1:11434")
    with pytest.raises(FatalError) as exc:
        await gw.call(_req(provider="anthropic"))
    assert "provider_class_not_allowed" in str(exc.value)
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_fake_provider_never_opens_socket(monkeypatch) -> None:
    """provider='fake' dispatches deterministically with zero HTTP (invariants 3, 10)."""
    import httpx
    import socket

    class _BombClient:
        def __init__(self, *a, **k) -> None:
            raise AssertionError("fake provider must not touch httpx")

    class _BombSocket:
        def __init__(self, *a, **k) -> None:
            raise AssertionError("fake provider must not touch sockets")

    monkeypatch.setattr(httpx, "AsyncClient", _BombClient)
    monkeypatch.setattr(socket, "socket", _BombSocket)
    gw = LocalGateway(base_url="http://127.0.0.1:11434")
    result = await gw.call(_req(provider="fake"))
    assert result.provider == "fake"
    assert result.provider_class == "local"
    assert isinstance(result.content, str)


@pytest.mark.asyncio
async def test_timeout_classifies_as_retryable(monkeypatch) -> None:
    import httpx

    class _TimeoutClient:
        def __init__(self, *a, **k) -> None: pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            raise httpx.ReadTimeout("simulated timeout")

    monkeypatch.setattr(httpx, "AsyncClient", _TimeoutClient)
    gw = LocalGateway(base_url="http://127.0.0.1:11434")
    with pytest.raises(RetryableError):
        await gw.call(_req(provider="local"))


@pytest.mark.asyncio
async def test_connection_refused_classifies_as_retryable(monkeypatch) -> None:
    import httpx

    class _ConnClient:
        def __init__(self, *a, **k) -> None: pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _ConnClient)
    gw = LocalGateway(base_url="http://127.0.0.1:11434")
    with pytest.raises(RetryableError):
        await gw.call(_req(provider="local"))


@pytest.mark.asyncio
async def test_model_not_found_classifies_as_fatal(monkeypatch) -> None:
    import httpx

    class _Response:
        status_code = 404
        text = '{"error":"model not found"}'
        def json(self) -> dict:
            return {"error": 'model "nope:1b" not found'}

    class _MissingModelClient:
        def __init__(self, *a, **k) -> None: pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _MissingModelClient)
    gw = LocalGateway(base_url="http://127.0.0.1:11434")
    with pytest.raises(FatalError) as exc:
        await gw.call(_req(provider="local", model="nope:1b"))
    assert "model_not_found" in str(exc.value)


@pytest.mark.asyncio
async def test_malformed_response_classifies_as_fatal(monkeypatch) -> None:
    import httpx

    class _Response:
        status_code = 200
        text = "not json"
        def json(self) -> dict:
            import json as _json
            raise _json.JSONDecodeError("err", "doc", 0)

    class _Client:
        def __init__(self, *a, **k) -> None: pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    gw = LocalGateway(base_url="http://127.0.0.1:11434")
    with pytest.raises(FatalError) as exc:
        await gw.call(_req(provider="local"))
    assert "malformed_response" in str(exc.value)


@pytest.mark.asyncio
async def test_5xx_classifies_as_retryable(monkeypatch) -> None:
    import httpx

    class _Response:
        status_code = 503
        text = "upstream busy"
        def json(self) -> dict:
            return {"error": "busy"}

    class _Client:
        def __init__(self, *a, **k) -> None: pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    gw = LocalGateway(base_url="http://127.0.0.1:11434")
    with pytest.raises(RetryableError):
        await gw.call(_req(provider="local"))


@pytest.mark.asyncio
async def test_happy_path_returns_local_council_model_result(monkeypatch) -> None:
    import httpx

    class _Response:
        status_code = 200
        text = ""
        def json(self) -> dict:
            return {
                "message": {"role": "assistant", "content": "hello"},
                "prompt_eval_count": 10,
                "eval_count": 3,
                "total_duration": 5_000_000,
            }

    class _Client:
        def __init__(self, *a, **k) -> None: pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    gw = LocalGateway(base_url="http://127.0.0.1:11434")
    result = await gw.call(_req(provider="local", model="llama3.2:1b"))
    assert result.content == "hello"
    assert result.provider == "ollama"
    assert result.provider_class == "local"
    assert result.input_tokens == 10
    assert result.output_tokens == 3
    assert result.duration_ms == 5
    assert result.raw_usage_available is True


@pytest.mark.asyncio
async def test_nullable_usage_when_field_missing(monkeypatch) -> None:
    import httpx

    class _Response:
        status_code = 200
        text = ""
        def json(self) -> dict:
            return {"message": {"role": "assistant", "content": "hi"}}

    class _Client:
        def __init__(self, *a, **k) -> None: pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    gw = LocalGateway(base_url="http://127.0.0.1:11434")
    result = await gw.call(_req(provider="local"))
    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.raw_usage_available is False
