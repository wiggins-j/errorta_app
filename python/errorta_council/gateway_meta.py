"""Narrow read-only adapter over F030 route/catalog metadata.

Phase 0 ships:

- ``GatewayMeta`` Protocol — the only contract validation depends on.
- ``FakeGatewayMeta`` — deterministic in-memory fake for tests.
- ``RealGatewayMeta`` — thin stub that returns ``"unknown"`` until F030
  grows a real route lookup. Wired into the production validator path
  by ``validate_room`` so Phase 0 cannot read provider state by accident.

This module **never** calls a provider (invariant 3: gateway is the only
egress, and Council never imports a provider SDK).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class GatewayMeta(Protocol):
    catalog_version: str | None

    def get_route(self, route_id: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class FakeGatewayMeta:
    known_routes: dict[str, dict[str, Any]]
    catalog_version: str | None = None

    def get_route(self, route_id: str) -> dict[str, Any] | None:
        if route_id in self.known_routes:
            return self.known_routes[route_id]
        # Synthetic fake routes (used by Council seed fixtures and the
        # fake-mode UI path) follow the convention ``fake.<class>.<model>``
        # and are always treated as local + unpriced. This keeps the Phase 1
        # readiness gate honest without needing every fake.* model to be
        # registered in the catalog.
        if route_id.startswith("fake.") or route_id.startswith("local."):
            return {"kind": "local", "priced": False}
        return None


@dataclass(frozen=True)
class RealGatewayMeta:
    """Production route lookup.

    F034 (2026-06-12): bridges the F034 async-provider registry. Local
    + fake routes are recognized by prefix; anthropic / openai / google
    / custom routes are checked against
    ``async_registry.list_provider_classes()`` so a route's provider
    class must be registered for the route to be considered known.
    Custom routes additionally require an entry in the provider-keys
    store with the matching alias.
    """

    catalog_version: str | None = None

    def get_route(self, route_id: str) -> dict[str, Any] | None:  # noqa: D401
        if not route_id:
            return None
        # Local + fake — always known. No provider call here.
        if route_id.startswith("fake.") or route_id.startswith("local."):
            return {"kind": "local", "priced": False}
        # Split provider class from the rest. Format <class>.<rest>.
        head = route_id.split(".", 1)[0]
        if not head:
            return None
        # Import lazily to avoid any chance of an import cycle at module
        # load — gateway_meta is imported from validation.py, which the
        # F034 dispatcher transitively touches.
        try:
            from errorta_model_gateway.providers import async_registry
        except Exception:
            return None
        async_registry.ensure_bootstrapped()
        handler = async_registry.get_handler(head)
        if handler is None:
            return None
        if head == "custom":
            # custom.<alias> requires a matching provider-keys entry.
            alias = route_id[len("custom."):]
            try:
                from errorta_app import provider_keys
                if provider_keys.get_custom_entry(alias) is None:
                    return None
            except Exception:
                return None
            return {"kind": "remote", "priced": True}
        # Fixed remote providers — recognize as remote+priced. The
        # validator surfaces this to gate budget checks.
        if head in ("anthropic", "openai", "google"):
            return {"kind": "remote", "priced": True}
        if head in ("local", "fake"):
            return {"kind": "local", "priced": False}
        # Unknown provider class is registered but not classified —
        # treat as remote conservatively.
        return {"kind": "remote", "priced": True}
