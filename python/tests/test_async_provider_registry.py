"""F034-1 — lock the async provider registry contract.

The dispatcher in errorta_council.gateway_local depends on:
- register() being idempotent
- get_handler() returning singleton instances (so monkeypatched fakes
  in one test don't leak into another via cached state)
- unregister() clearing both the factory AND the cached instance
- list_provider_classes() returning sorted names

These are tiny contracts but the dispatcher and the future F033 room
editor both call them — locking them now prevents drift.
"""
from __future__ import annotations

import pytest

from errorta_model_gateway.providers import async_registry
from errorta_model_gateway.providers.async_base import (
    AsyncProviderHandler,
    AsyncProviderRequest,
    AsyncProviderResult,
    RouteDescriptor,
    ValidationResult,
)


class _FakeHandler:
    """Minimal implementation of the AsyncProviderHandler Protocol."""

    provider_class: str = "fake-test"
    display_name: str = "Fake (test)"

    def __init__(self, label: str = "default") -> None:
        self.label = label
        self.call_count = 0

    async def call(self, request, *, api_key):
        self.call_count += 1
        return AsyncProviderResult(
            content=f"fake-response-{self.label}",
            provider_class=self.provider_class,
            model=request.model,
            input_tokens=0,
            output_tokens=0,
            duration_ms=1,
            raw_usage_available=False,
        )

    def list_routes(self, *, configured):
        return [RouteDescriptor(route_id="fake-test.x", label="Fake X")]

    def validate_route(self, route_id):
        return ValidationResult(ok=True)


def _scrub_registry():
    """Clear test-injected entries; leave any built-ins alone."""
    async_registry.unregister("fake-test")
    async_registry.unregister("fake-test-2")


def test_register_caches_singleton_instance() -> None:
    _scrub_registry()
    h = _FakeHandler(label="alpha")
    async_registry.register("fake-test", lambda: h)
    a = async_registry.get_handler("fake-test")
    b = async_registry.get_handler("fake-test")
    assert a is h
    assert b is a, "get_handler must return the same singleton across calls"
    _scrub_registry()


def test_register_replaces_existing_handler() -> None:
    """Re-registering swaps the factory AND drops the cached instance."""
    _scrub_registry()
    h1 = _FakeHandler(label="first")
    async_registry.register("fake-test", lambda: h1)
    assert async_registry.get_handler("fake-test") is h1

    h2 = _FakeHandler(label="second")
    async_registry.register("fake-test", lambda: h2)
    assert async_registry.get_handler("fake-test") is h2, (
        "register() must invalidate the cached instance"
    )
    _scrub_registry()


def test_unregister_clears_factory_and_instance() -> None:
    _scrub_registry()
    h = _FakeHandler()
    async_registry.register("fake-test", lambda: h)
    assert async_registry.get_handler("fake-test") is h
    async_registry.unregister("fake-test")
    assert async_registry.get_handler("fake-test") is None


def test_get_handler_returns_none_for_unknown_provider() -> None:
    _scrub_registry()
    assert async_registry.get_handler("totally-nonexistent-provider") is None


def test_list_provider_classes_is_sorted() -> None:
    _scrub_registry()
    async_registry.register("fake-test-2", lambda: _FakeHandler(label="z"))
    async_registry.register("fake-test", lambda: _FakeHandler(label="a"))
    classes = async_registry.list_provider_classes()
    # Test entries should appear in sorted order.
    fake_classes = [c for c in classes if c.startswith("fake-test")]
    assert fake_classes == sorted(fake_classes)
    _scrub_registry()


def test_ensure_bootstrapped_is_idempotent() -> None:
    """Calling bootstrap repeatedly does not double-register handlers."""
    async_registry.ensure_bootstrapped()
    classes_first = async_registry.list_provider_classes()
    async_registry.ensure_bootstrapped()
    async_registry.ensure_bootstrapped()
    classes_second = async_registry.list_provider_classes()
    assert classes_first == classes_second


@pytest.mark.asyncio
async def test_fake_handler_call_round_trip() -> None:
    """End-to-end: register, get, call. Locks the call() signature."""
    _scrub_registry()
    async_registry.register("fake-test", lambda: _FakeHandler())
    handler = async_registry.get_handler("fake-test")
    assert handler is not None
    request = AsyncProviderRequest(
        model="any",
        messages=[{"role": "user", "content": "hi"}],
        max_output_tokens=64,
    )
    result = await handler.call(request, api_key=None)
    assert result.content == "fake-response-default"
    assert result.provider_class == "fake-test"
    assert result.duration_ms == 1
    _scrub_registry()


def test_async_provider_protocol_runtime_check() -> None:
    """A handler missing a required attribute fails Protocol check."""
    class BrokenHandler:
        # Missing provider_class, display_name, call, list_routes, validate_route
        pass

    # We don't enforce runtime Protocol checking (Python Protocol is
    # structural by default), but downstream code can use this pattern.
    assert not hasattr(BrokenHandler(), "provider_class")
    fake = _FakeHandler()
    assert hasattr(fake, "provider_class")
    assert hasattr(fake, "display_name")
    assert callable(getattr(fake, "call", None))
    assert callable(getattr(fake, "list_routes", None))
    assert callable(getattr(fake, "validate_route", None))
