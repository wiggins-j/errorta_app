"""F034-8 — Council gateway dispatcher locks.

LocalGateway.call() must:
1. Route ``anthropic.*`` to the AnthropicHandler from the registry.
2. Route ``openai.*`` to the OpenAIHandler.
3. Route ``google.*`` to the GoogleHandler.
4. Route ``custom.*`` to the CustomHandler.
5. Fall through to the legacy local/fake dispatch for ``local.*`` /
   ``fake.*`` route_ids AND for arbitrary unprefixed route_ids
   (preserves backward compat with the existing test suite).
6. Use ``provider_keys.get_fixed_key`` to resolve the API key.
7. Re-check ``verify_payload_route_alignment`` correctly for remote
   destination_scope.

These tests inject a fake AsyncProviderHandler via the registry so no
real httpx call escapes the dispatcher.
"""
from __future__ import annotations

import pytest

from errorta_council.gateway_local import (
    FatalError,
    LocalCouncilModelRequest,
    LocalCouncilModelResult,
    LocalGateway,
    _is_local_route,
    _provider_class_from_route_id,
    verify_payload_route_alignment,
)
from errorta_model_gateway.providers import async_registry
from errorta_model_gateway.providers.async_base import (
    AsyncProviderRequest,
    AsyncProviderResult,
    RouteDescriptor,
    ValidationResult,
)


# ----------------------------------------------------------------------
# Helpers / fixtures
# ----------------------------------------------------------------------


class _CapturingHandler:
    """Fake AsyncProviderHandler that records inputs and returns a fixed result."""

    def __init__(self, provider_class: str) -> None:
        self.provider_class = provider_class
        self.display_name = f"Capturing-{provider_class}"
        self.calls: list[tuple[AsyncProviderRequest, str | None]] = []

    async def call(self, request, *, api_key):
        self.calls.append((request, api_key))
        return AsyncProviderResult(
            content=f"reply-from-{self.provider_class}",
            provider_class=self.provider_class,
            model=request.model,
            input_tokens=11,
            output_tokens=7,
            duration_ms=42,
            raw_usage_available=True,
        )

    def list_routes(self, *, configured):
        return [RouteDescriptor(route_id=f"{self.provider_class}.x", label="X")]

    def validate_route(self, route_id):
        return ValidationResult(ok=True)


@pytest.fixture(autouse=True)
def _isolate_home_and_restore_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    # Force the registry to bootstrap NOW (idempotent) so the real
    # handler modules are loaded before any test injects fakes.
    async_registry.ensure_bootstrapped()
    yield
    # Restore the real handler factories after the test. Since
    # ensure_bootstrapped is one-shot (_BOOTSTRAPPED stays True after
    # the first call), tests that unregister + ensure_bootstrapped
    # leave the registry without that handler. Re-register the real
    # factories directly to keep cross-test isolation.
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


def _request(route_id: str, *, destination_scope: str = "remote", egress_class: str = "remote_eligible") -> LocalCouncilModelRequest:
    return LocalCouncilModelRequest(
        role="member",
        route_id=route_id,
        provider="local",  # the legacy field — ignored when route_id has a remote prefix
        model="ignored",
        messages=[{"role": "user", "content": "hi"}],
        max_output_tokens=64,
        temperature=0.0,
        timeout_seconds=30,
        metadata={
            "context_id": "ctx",
            "member_id": "m",
            "destination_scope": destination_scope,
            "egress_class": egress_class,
        },
    )


# ----------------------------------------------------------------------
# Provider class extraction
# ----------------------------------------------------------------------


def test_provider_class_from_route_id_dot_form() -> None:
    assert _provider_class_from_route_id("anthropic.claude-sonnet-4-6") == "anthropic"
    assert _provider_class_from_route_id("openai.gpt-4o") == "openai"
    assert _provider_class_from_route_id("google.gemini-1.5-pro") == "google"
    assert _provider_class_from_route_id("custom.lmstudio") == "custom"
    assert _provider_class_from_route_id("local.ollama.llama3.2:3b") == "local"
    assert _provider_class_from_route_id("fake.local.deterministic") == "fake"


def test_provider_class_from_route_id_slash_form() -> None:
    assert _provider_class_from_route_id("local/ollama/llama3.2:3b") == "local"


def test_provider_class_from_route_id_empty() -> None:
    assert _provider_class_from_route_id("") == ""


def test_is_local_route() -> None:
    assert _is_local_route("local.ollama.llama3.2:3b") is True
    assert _is_local_route("fake.local.deterministic") is True
    assert _is_local_route("local/ollama/llama3.2:3b") is True
    assert _is_local_route("anthropic.claude-sonnet-4-6") is False
    assert _is_local_route("") is False


# ----------------------------------------------------------------------
# Boundary re-check (invariant 5) — F034 widening
# ----------------------------------------------------------------------


def test_verify_payload_route_alignment_local_scope_local_route_ok() -> None:
    verify_payload_route_alignment(
        destination_scope="local",
        egress_class="local",
        route_id="local.ollama.llama3.2:3b",
    )  # no raise


def test_verify_payload_route_alignment_remote_scope_remote_route_ok() -> None:
    verify_payload_route_alignment(
        destination_scope="remote",
        egress_class="remote_eligible",
        route_id="anthropic.claude-sonnet-4-6",
    )  # no raise


def test_verify_payload_route_alignment_remote_scope_local_route_raises() -> None:
    with pytest.raises(FatalError, match="payload_route_mismatch"):
        verify_payload_route_alignment(
            destination_scope="remote",
            egress_class="remote_eligible",
            route_id="local.ollama.llama3.2:3b",
        )


def test_verify_payload_route_alignment_local_scope_remote_route_raises() -> None:
    with pytest.raises(FatalError, match="payload_route_mismatch"):
        verify_payload_route_alignment(
            destination_scope="local",
            egress_class="local",
            route_id="anthropic.claude-sonnet-4-6",
        )


# ----------------------------------------------------------------------
# Dispatcher — registry dispatch for remote providers
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_route_dispatches_to_registry(monkeypatch) -> None:
    from errorta_app import provider_keys

    provider_keys.upsert_fixed("anthropic", "sk-ant-test-1234")
    handler = _CapturingHandler("anthropic")
    async_registry.register("anthropic", lambda: handler)

    try:
        gateway = LocalGateway()
        request = _request("anthropic.claude-sonnet-4-6")
        result = await gateway.call(request)
        assert isinstance(result, LocalCouncilModelResult)
        assert result.content == "reply-from-anthropic"
        assert result.provider_class == "anthropic"
        assert result.input_tokens == 11
        assert len(handler.calls) == 1
        captured_req, captured_key = handler.calls[0]
        # Provider-side model = route_id with the `anthropic.` prefix stripped.
        assert captured_req.model == "claude-sonnet-4-6"
        assert captured_key == "sk-ant-test-1234"
    finally:
        # Restore the real handler.
        async_registry.unregister("anthropic")
        # Re-bootstrap so the real handler comes back for subsequent tests.
        async_registry.ensure_bootstrapped()


@pytest.mark.asyncio
async def test_openai_route_dispatches_to_registry() -> None:
    from errorta_app import provider_keys

    provider_keys.upsert_fixed("openai", "sk-openai-xyz")
    handler = _CapturingHandler("openai")
    async_registry.register("openai", lambda: handler)
    try:
        gateway = LocalGateway()
        request = _request("openai.gpt-4o")
        result = await gateway.call(request)
        assert result.content == "reply-from-openai"
        assert handler.calls[0][1] == "sk-openai-xyz"
        assert handler.calls[0][0].model == "gpt-4o"
    finally:
        async_registry.unregister("openai")
        async_registry.ensure_bootstrapped()


@pytest.mark.asyncio
async def test_google_route_dispatches_to_registry() -> None:
    from errorta_app import provider_keys

    provider_keys.upsert_fixed("google", "goog-key-aaa")
    handler = _CapturingHandler("google")
    async_registry.register("google", lambda: handler)
    try:
        gateway = LocalGateway()
        request = _request("google.gemini-1.5-pro")
        result = await gateway.call(request)
        assert result.content == "reply-from-google"
        assert handler.calls[0][1] == "goog-key-aaa"
        assert handler.calls[0][0].model == "gemini-1.5-pro"
    finally:
        async_registry.unregister("google")
        async_registry.ensure_bootstrapped()


@pytest.mark.asyncio
async def test_custom_route_dispatches_to_registry_without_key_param() -> None:
    """Custom handler reads its config from the provider-keys store
    by alias; the api_key parameter from the dispatcher is None.
    """
    handler = _CapturingHandler("custom")
    async_registry.register("custom", lambda: handler)
    try:
        gateway = LocalGateway()
        request = _request("custom.lmstudio")
        result = await gateway.call(request)
        assert result.content == "reply-from-custom"
        captured_req, captured_key = handler.calls[0]
        assert captured_req.model == "lmstudio"  # alias passed as model
        assert captured_key is None
    finally:
        async_registry.unregister("custom")
        async_registry.ensure_bootstrapped()


@pytest.mark.asyncio
async def test_unknown_remote_provider_class_falls_through_to_legacy_path() -> None:
    """A route_id like ``r1`` (no prefix) or ``foo.bar`` (no handler) must
    fall through to the legacy provider-field dispatch — backward compat
    for the existing test suite.
    """
    gateway = LocalGateway()
    # The legacy provider="local" path needs Ollama at 11434, which we
    # don't have — so the actual call would RetryableError. We don't
    # care about the failure mode; we only care it didn't try the
    # registry. Route through a path that errors before HTTP.
    request = LocalCouncilModelRequest(
        role="member",
        route_id="r1",  # no recognized prefix
        provider="totally-unknown-legacy-provider",
        model="x",
        messages=[],
        max_output_tokens=1,
        temperature=0,
        timeout_seconds=1,
        metadata={},
    )
    # Should hit the "provider_class_not_allowed" error from the
    # legacy path — proving registry dispatch was NOT taken.
    with pytest.raises(FatalError, match="provider_class_not_allowed"):
        await gateway.call(request)


@pytest.mark.asyncio
async def test_remote_dispatch_records_provider_class_on_result() -> None:
    """The wrapped LocalCouncilModelResult must carry provider_class for
    downstream introspection (audit, transcript events).
    """
    from errorta_app import provider_keys

    provider_keys.upsert_fixed("anthropic", "sk-key")
    handler = _CapturingHandler("anthropic")
    async_registry.register("anthropic", lambda: handler)
    try:
        gateway = LocalGateway()
        result = await gateway.call(_request("anthropic.claude-sonnet-4-6"))
        assert result.provider == "anthropic"
        assert result.provider_class == "anthropic"
        assert result.model == "claude-sonnet-4-6"
        assert result.duration_ms == 42
    finally:
        async_registry.unregister("anthropic")
        async_registry.ensure_bootstrapped()
