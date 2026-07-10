"""F034 — Async provider handler registry.

Open-ended. Adding a new provider is a new module + one line in
``PROVIDER_HANDLERS``.

The dispatcher in ``errorta_council.gateway_local`` parses route_ids of
the form ``<provider_class>.<rest>``, looks up the handler by
``provider_class``, and forwards the call. Route_ids starting with
``local.`` or ``fake.`` still route through the legacy local handler
for backward compat — see the dispatcher for the transition rule.
"""
from __future__ import annotations

from typing import Callable

from .async_base import AsyncProviderHandler


# Built-in handler factory. The registry holds factories (zero-arg
# callables) rather than instances so handlers can be swapped at test
# time via monkeypatch without affecting other tests.
#
# Population happens at first ``get_handler`` call, not at import time,
# so importing this module does not pull every handler's httpx-using
# code path into sys.modules eagerly.
_FACTORIES: dict[str, Callable[[], AsyncProviderHandler]] = {}
_INSTANCES: dict[str, AsyncProviderHandler] = {}


def register(provider_class: str, factory: Callable[[], AsyncProviderHandler]) -> None:
    """Register a handler factory under ``provider_class``.

    Idempotent: re-registering replaces the existing factory and clears
    any cached instance. Used at module-import time by each handler.
    """
    _FACTORIES[provider_class] = factory
    _INSTANCES.pop(provider_class, None)


def unregister(provider_class: str) -> None:
    """Remove a handler. Test-only — never called in production."""
    _FACTORIES.pop(provider_class, None)
    _INSTANCES.pop(provider_class, None)


def get_handler(provider_class: str) -> AsyncProviderHandler | None:
    """Return the singleton handler for ``provider_class``, or None."""
    if provider_class in _INSTANCES:
        return _INSTANCES[provider_class]
    factory = _FACTORIES.get(provider_class)
    if factory is None:
        return None
    instance = factory()
    _INSTANCES[provider_class] = instance
    return instance


def list_provider_classes() -> list[str]:
    """All registered provider classes, sorted alphabetically."""
    return sorted(_FACTORIES.keys())


# ----------------------------------------------------------------------
# Built-in registration. Each handler module calls ``register`` at
# import time. We import them here so a single ``import async_registry``
# pulls in the full default set without the caller knowing the module
# names.
#
# Imports are at the BOTTOM to avoid an import cycle (each handler
# imports ``async_base`` from the same package).
# ----------------------------------------------------------------------

def _bootstrap() -> None:
    """Import every built-in handler module to trigger their ``register`` calls.

    Idempotent. Called lazily on first ``get_handler`` lookup that
    misses, AND eagerly at module import below. Either path is fine —
    handlers' ``register`` calls are themselves idempotent.
    """
    # The imports below are intentionally inside this function so that
    # ``async_registry`` can be imported by handler modules without
    # creating a cycle.
    from . import (  # noqa: F401 — imports for side-effect registration
        async_anthropic,
        async_claude_cli,
        async_codex_cli,
        async_cursor_cli,
        async_custom,
        async_google,
        async_local,
        async_openai,
    )


_BOOTSTRAPPED = False


def ensure_bootstrapped() -> None:
    """Run ``_bootstrap`` once. Safe to call repeatedly."""
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    try:
        _bootstrap()
    except ImportError:
        # One or more handler modules are not yet built — that's fine
        # during incremental development. The registry remains populated
        # with whatever did import cleanly.
        pass
    _BOOTSTRAPPED = True


__all__ = [
    "register",
    "unregister",
    "get_handler",
    "list_provider_classes",
    "ensure_bootstrapped",
]
