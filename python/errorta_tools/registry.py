"""Registry for ToolGateway handlers.

Mirrors ``errorta_model_gateway.providers.async_registry`` but deliberately
has no built-in imports in slice 1. Later MCP-backed tools can register their
factories from their own modules without making Council import egress code.
"""
from __future__ import annotations

from typing import Callable

from .gateway import ToolHandler

_FACTORIES: dict[str, Callable[[], ToolHandler]] = {}
_INSTANCES: dict[str, ToolHandler] = {}


def register(tool_id: str, factory: Callable[[], ToolHandler]) -> None:
    """Register a handler factory under a stable tool id."""
    _FACTORIES[tool_id] = factory
    _INSTANCES.pop(tool_id, None)


def unregister(tool_id: str) -> None:
    """Remove a handler. Test-only until real tools ship."""
    _FACTORIES.pop(tool_id, None)
    _INSTANCES.pop(tool_id, None)


def clear() -> None:
    """Clear all registered handlers. Test-only."""
    _FACTORIES.clear()
    _INSTANCES.clear()


def get_handler(tool_id: str) -> ToolHandler | None:
    """Return the singleton handler for ``tool_id`` if registered."""
    if tool_id in _INSTANCES:
        return _INSTANCES[tool_id]
    factory = _FACTORIES.get(tool_id)
    if factory is None:
        return None
    instance = factory()
    _INSTANCES[tool_id] = instance
    return instance


def list_tool_ids() -> list[str]:
    return sorted(_FACTORIES.keys())


__all__ = ["register", "unregister", "clear", "get_handler", "list_tool_ids"]
