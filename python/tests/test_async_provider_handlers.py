"""F034-3..F034-6 — provider handler integration tests.

One section per handler. All HTTP is mocked via ``httpx.MockTransport``
so the tests are hermetic. Each section locks:

- Request shape (model + messages + system handling).
- Headers (auth correctness).
- 4xx → FatalError, 5xx + 429 + timeouts → RetryableError.
- Token usage normalization.
- Missing API key → FatalError (no HTTP attempt).
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from errorta_model_gateway.providers.async_base import AsyncProviderRequest
from errorta_model_gateway.providers.async_anthropic import AnthropicHandler
from errorta_model_gateway.providers.async_openai import OpenAIHandler
from errorta_model_gateway.providers.async_google import GoogleHandler
from errorta_model_gateway.providers.async_custom import CustomHandler


# ----------------------------------------------------------------------
# Shared fixtures
# ----------------------------------------------------------------------


def _ok_anthropic_body() -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Hello from Claude."},
        ],
        "model": "claude-sonnet-4-6",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 5},
    }


def _ok_openai_body() -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello from GPT."},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
    }


def _ok_google_body() -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": "Hello from Gemini."}],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 8,
            "candidatesTokenCount": 4,
            "totalTokenCount": 12,
        },
    }


def _patch_httpx_async_client(monkeypatch, *, transport):
    """Replace httpx.AsyncClient so handlers route through our mock."""
    original = httpx.AsyncClient

    class _MockAsyncClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _MockAsyncClient)


# ======================================================================
# Anthropic handler
# ======================================================================


@pytest.mark.asyncio
async def test_anthropic_happy_path(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_anthropic_body())

    _patch_httpx_async_client(monkeypatch, transport=httpx.MockTransport(_handler))
    handler = AnthropicHandler()
    req = AsyncProviderRequest(
        model="claude-sonnet-4-6",
        messages=[
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hello."},
        ],
        max_output_tokens=128,
    )
    result = await handler.call(req, api_key="sk-ant-test")
    assert result.content == "Hello from Claude."
    assert result.input_tokens == 12
    assert result.output_tokens == 5
    assert result.raw_usage_available is True
    assert result.provider_class == "anthropic"
    # System hoisted out of messages.
    assert captured["body"]["system"] == "Be concise."
    assert captured["body"]["messages"] == [
        {"role": "user", "content": "Hello."}
    ]
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_anthropic_missing_key_raises_fatal() -> None:
    from errorta_council.gateway_local import FatalError

    handler = AnthropicHandler()
    req = AsyncProviderRequest(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "x"}],
    )
    with pytest.raises(FatalError, match="anthropic_missing_api_key"):
        await handler.call(req, api_key=None)


@pytest.mark.asyncio
async def test_anthropic_401_raises_fatal_auth(monkeypatch) -> None:
    from errorta_council.gateway_local import FatalError

    _patch_httpx_async_client(
        monkeypatch,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(401, json={"error": "auth"}),
        ),
    )
    handler = AnthropicHandler()
    req = AsyncProviderRequest(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "x"}],
    )
    with pytest.raises(FatalError, match="anthropic_auth_failed"):
        await handler.call(req, api_key="bad-key")


@pytest.mark.asyncio
async def test_anthropic_500_raises_retryable(monkeypatch) -> None:
    from errorta_council.gateway_local import RetryableError

    _patch_httpx_async_client(
        monkeypatch,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(503, json={"error": "down"}),
        ),
    )
    handler = AnthropicHandler()
    req = AsyncProviderRequest(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "x"}],
    )
    with pytest.raises(RetryableError, match="anthropic_provider_5xx"):
        await handler.call(req, api_key="sk-ant-test")


@pytest.mark.asyncio
async def test_anthropic_429_raises_retryable(monkeypatch) -> None:
    from errorta_council.gateway_local import RetryableError

    _patch_httpx_async_client(
        monkeypatch,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(429, json={"error": "slow down"}),
        ),
    )
    handler = AnthropicHandler()
    req = AsyncProviderRequest(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "x"}],
    )
    with pytest.raises(RetryableError, match="anthropic_rate_limited"):
        await handler.call(req, api_key="sk-ant-test")


def test_anthropic_list_routes_returns_curated_catalog() -> None:
    routes = AnthropicHandler().list_routes(configured=True)
    assert any(r.route_id == "anthropic.claude-sonnet-4-6" for r in routes)
    assert any(r.route_id == "anthropic.claude-opus-4-8" for r in routes)


def test_anthropic_validate_route_prefix_required() -> None:
    h = AnthropicHandler()
    assert h.validate_route("anthropic.claude-sonnet-4-6").ok is True
    assert h.validate_route("openai.gpt-4o").ok is False
    assert h.validate_route("anthropic.").ok is False


# ======================================================================
# OpenAI handler
# ======================================================================


@pytest.mark.asyncio
async def test_openai_happy_path(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json=_ok_openai_body())

    _patch_httpx_async_client(monkeypatch, transport=httpx.MockTransport(_handler))
    handler = OpenAIHandler()
    req = AsyncProviderRequest(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hello."},
        ],
        max_output_tokens=128,
    )
    result = await handler.call(req, api_key="sk-test-openai")
    assert result.content == "Hello from GPT."
    assert result.input_tokens == 10
    assert result.output_tokens == 4
    assert result.raw_usage_available is True
    assert captured["headers"]["authorization"] == "Bearer sk-test-openai"
    # System messages pass through; OpenAI handles them natively.
    assert captured["body"]["messages"][0]["role"] == "system"


@pytest.mark.asyncio
async def test_openai_missing_key_raises_fatal() -> None:
    from errorta_council.gateway_local import FatalError

    handler = OpenAIHandler()
    req = AsyncProviderRequest(
        model="gpt-4o", messages=[{"role": "user", "content": "x"}]
    )
    with pytest.raises(FatalError, match="openai_missing_api_key"):
        await handler.call(req, api_key=None)


@pytest.mark.asyncio
async def test_openai_404_raises_fatal_model_not_found(monkeypatch) -> None:
    from errorta_council.gateway_local import FatalError

    _patch_httpx_async_client(
        monkeypatch,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(404, json={"error": "model not found"}),
        ),
    )
    handler = OpenAIHandler()
    req = AsyncProviderRequest(
        model="gpt-totally-fake",
        messages=[{"role": "user", "content": "x"}],
    )
    with pytest.raises(FatalError, match="openai_model_not_found"):
        await handler.call(req, api_key="sk-test")


# ======================================================================
# Google (Gemini) handler
# ======================================================================


@pytest.mark.asyncio
async def test_google_happy_path(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json=_ok_google_body())

    _patch_httpx_async_client(monkeypatch, transport=httpx.MockTransport(_handler))
    handler = GoogleHandler()
    req = AsyncProviderRequest(
        model="gemini-1.5-pro",
        messages=[
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hello."},
        ],
        max_output_tokens=128,
    )
    result = await handler.call(req, api_key="goog-key-abc")
    assert result.content == "Hello from Gemini."
    assert result.input_tokens == 8
    assert result.output_tokens == 4
    # API key in query string per Google convention.
    assert "key=goog-key-abc" in captured["url"]
    # System hoisted to systemInstruction.
    assert captured["body"]["systemInstruction"]["parts"][0]["text"] == "Be concise."
    # contents[0] is the user message; role rewritten.
    assert captured["body"]["contents"][0]["role"] == "user"
    assert captured["body"]["contents"][0]["parts"][0]["text"] == "Hello."


@pytest.mark.asyncio
async def test_google_assistant_role_becomes_model(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json=_ok_google_body())

    _patch_httpx_async_client(monkeypatch, transport=httpx.MockTransport(_handler))
    handler = GoogleHandler()
    req = AsyncProviderRequest(
        model="gemini-1.5-pro",
        messages=[
            {"role": "user", "content": "first turn"},
            {"role": "assistant", "content": "first response"},
            {"role": "user", "content": "second turn"},
        ],
    )
    await handler.call(req, api_key="goog-key")
    roles = [m["role"] for m in captured["body"]["contents"]]
    assert roles == ["user", "model", "user"]


# ======================================================================
# Custom handler
# ======================================================================


@pytest.fixture
def _isolate_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    yield


@pytest.mark.asyncio
async def test_custom_openai_compat_path(_isolate_keys, monkeypatch) -> None:
    from errorta_app import provider_keys

    provider_keys.upsert_custom({
        "alias": "lmstudio",
        "base_url": "http://127.0.0.1:1234/v1",
        "api_key": "lm-secret",
        "api_style": "openai_chat_completions",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "model": "qwen2.5-coder-7b",
    })

    captured: dict[str, Any] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json=_ok_openai_body())

    _patch_httpx_async_client(monkeypatch, transport=httpx.MockTransport(_handler))
    handler = CustomHandler()
    request = AsyncProviderRequest(
        model="lmstudio",  # the alias
        messages=[{"role": "user", "content": "hi"}],
    )
    result = await handler.call(request, api_key=None)
    assert result.content == "Hello from GPT."
    assert captured["url"] == "http://127.0.0.1:1234/v1/chat/completions"
    # wire_model is read from the entry's `model` field, not the alias.
    assert captured["body"]["model"] == "qwen2.5-coder-7b"
    assert captured["headers"]["authorization"] == "Bearer lm-secret"


@pytest.mark.asyncio
async def test_custom_anthropic_compat_path(_isolate_keys, monkeypatch) -> None:
    from errorta_app import provider_keys

    provider_keys.upsert_custom({
        "alias": "claude-relay",
        "base_url": "https://relay.example.com",
        "api_key": "relay-token",
        "api_style": "anthropic_messages",
        "auth_header": "x-api-key",
        "auth_prefix": "",
    })

    captured: dict[str, Any] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json=_ok_anthropic_body())

    _patch_httpx_async_client(monkeypatch, transport=httpx.MockTransport(_handler))
    handler = CustomHandler()
    request = AsyncProviderRequest(
        model="claude-relay",
        messages=[
            {"role": "system", "content": "You are a relay."},
            {"role": "user", "content": "ping"},
        ],
        max_output_tokens=64,
    )
    result = await handler.call(request, api_key=None)
    assert result.content == "Hello from Claude."
    assert captured["url"] == "https://relay.example.com/messages"
    assert captured["headers"]["x-api-key"] == "relay-token"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["body"]["system"] == "You are a relay."


@pytest.mark.asyncio
async def test_custom_missing_alias_raises_fatal(_isolate_keys) -> None:
    from errorta_council.gateway_local import FatalError

    handler = CustomHandler()
    request = AsyncProviderRequest(
        model="not-configured",
        messages=[{"role": "user", "content": "x"}],
    )
    with pytest.raises(FatalError, match="custom_alias_not_found"):
        await handler.call(request, api_key=None)


def test_custom_list_routes_reads_configured_entries(_isolate_keys) -> None:
    from errorta_app import provider_keys

    provider_keys.upsert_custom({
        "alias": "lmstudio",
        "base_url": "http://127.0.0.1:1234/v1",
        "api_key": "k",
        "api_style": "openai_chat_completions",
    })
    provider_keys.upsert_custom({
        "alias": "vllm",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key": "k",
        "api_style": "openai_chat_completions",
        "model": "llama-3-70b",
    })
    routes = CustomHandler().list_routes(configured=True)
    aliases = sorted(r.route_id for r in routes)
    assert aliases == ["custom.lmstudio", "custom.vllm"]
