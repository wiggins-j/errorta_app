"""A transient decompression failure from a provider must normalize to
``RetryableError`` at the gateway egress choke point — never escape as a raw
``zlib.error`` (which crashed a whole coding run once: ``run_state.last_error``
= ``"error: Error -3 while decompressing data: incorrect header check"``,
because ``zlib.error.__name__`` is the bare ``"error"``).

The gateway is the SOLE Council egress (invariant 3), so normalizing here covers
every provider path. Fatal errors (auth, model-rejected, policy) must still
propagate as ``FatalError`` — only transient decode/wire failures are reclassified.
"""
from __future__ import annotations

import zlib

import httpx
import pytest

from errorta_council.gateway_local import (
    FatalError,
    LocalCouncilModelRequest,
    LocalGateway,
    RetryableError,
)
from errorta_model_gateway.providers import async_registry
from errorta_model_gateway.providers.async_base import (
    RouteDescriptor,
    ValidationResult,
)


class _RaisingHandler:
    """Fake AsyncProviderHandler whose ``call`` raises a preset exception."""

    def __init__(self, provider_class: str, exc: BaseException) -> None:
        self.provider_class = provider_class
        self.display_name = f"Raising-{provider_class}"
        self._exc = exc

    async def call(self, request, *, api_key):  # noqa: ANN001, ARG002
        raise self._exc

    def list_routes(self, *, configured):  # noqa: ANN001, ARG002
        return [RouteDescriptor(route_id=f"{self.provider_class}.x", label="X")]

    def validate_route(self, route_id):  # noqa: ANN001, ARG002
        return ValidationResult(ok=True)


@pytest.fixture(autouse=True)
def _isolate_home_and_restore_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    async_registry.ensure_bootstrapped()
    yield
    from errorta_model_gateway.providers.async_anthropic import AnthropicHandler
    from errorta_model_gateway.providers.async_custom import CustomHandler
    from errorta_model_gateway.providers.async_google import GoogleHandler
    from errorta_model_gateway.providers.async_local import LocalHandler
    from errorta_model_gateway.providers.async_openai import OpenAIHandler
    async_registry.register("anthropic", AnthropicHandler)
    async_registry.register("openai", OpenAIHandler)
    async_registry.register("google", GoogleHandler)
    async_registry.register("local", LocalHandler)
    async_registry.register("custom", CustomHandler)


def _request() -> LocalCouncilModelRequest:
    return LocalCouncilModelRequest(
        role="member",
        route_id="anthropic.x",
        provider="local",  # legacy field — ignored for a remote-prefixed route
        model="ignored",
        messages=[{"role": "user", "content": "hi"}],
        max_output_tokens=64,
        temperature=0.0,
        timeout_seconds=30,
        metadata={
            "member_id": "m",
            "destination_scope": "remote",
            "egress_class": "remote_eligible",
        },
    )


@pytest.mark.asyncio
async def test_raw_zlib_error_normalizes_to_retryable() -> None:
    """The exact live failure: a provider leaks a raw ``zlib.error``."""
    boom = zlib.error("Error -3 while decompressing data: incorrect header check")
    async_registry.register("anthropic", lambda: _RaisingHandler("anthropic", boom))

    with pytest.raises(RetryableError) as ei:
        await LocalGateway().call(_request())

    assert "gateway_decode_error" in str(ei.value)
    # And crucially NOT the raw zlib.error that would crash the run.
    assert not isinstance(ei.value, zlib.error)


@pytest.mark.asyncio
async def test_httpx_decoding_error_normalizes_to_retryable() -> None:
    """httpx wraps most zlib failures in DecodingError — also transient."""
    boom = httpx.DecodingError("incorrect header check")
    async_registry.register("anthropic", lambda: _RaisingHandler("anthropic", boom))

    with pytest.raises(RetryableError) as ei:
        await LocalGateway().call(_request())
    assert "gateway_decode_error" in str(ei.value)


@pytest.mark.asyncio
async def test_fatal_error_still_propagates_unchanged() -> None:
    """A genuinely fatal provider error (auth/model) must NOT be swallowed or
    reclassified as retryable by the decode wrapper (task: don't touch fatals)."""
    boom = FatalError("anthropic_auth_failed: 401 — check API key")
    async_registry.register("anthropic", lambda: _RaisingHandler("anthropic", boom))

    with pytest.raises(FatalError) as ei:
        await LocalGateway().call(_request())
    assert "auth_failed" in str(ei.value)


@pytest.mark.asyncio
async def test_retryable_error_passes_through_unchanged() -> None:
    """A provider that already raised RetryableError (e.g. a 5xx) is not
    re-wrapped into a confusing ``gateway_decode_error`` message."""
    boom = RetryableError("anthropic_provider_5xx: 503")
    async_registry.register("anthropic", lambda: _RaisingHandler("anthropic", boom))

    with pytest.raises(RetryableError) as ei:
        await LocalGateway().call(_request())
    assert "provider_5xx" in str(ei.value)
    assert "gateway_decode_error" not in str(ei.value)
