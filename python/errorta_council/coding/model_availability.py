"""F129 server-side model-family and runtime availability enforcement."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .model_catalog import provider_class


@dataclass(frozen=True)
class RouteAvailability:
    route_id: str
    provider_family: str
    available: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def effective_family_allowlist(configured: set[str], explicit: list[str] | None) -> set[str]:
    return set(configured) if explicit is None else set(explicit).intersection(configured)


def project_availability(
    route_ids: list[str],
    *,
    configured_families: set[str],
    enabled_families: set[str],
    cli_connected: dict[str, bool | None] | None = None,
    local_models: set[str] | None = None,
    ollama_reachable: bool = True,
) -> dict[str, RouteAvailability]:
    cli_connected = cli_connected or {}
    local_models = local_models or set()
    out: dict[str, RouteAvailability] = {}
    for route_id in route_ids:
        family = provider_class(route_id)
        reason = ""
        available = True
        if family not in enabled_families:
            available, reason = False, "family_disabled"
        elif family not in configured_families:
            available, reason = False, "provider_not_configured"
        elif family in {"claude_cli", "codex_cli", "cursor_cli"}:
            connected = cli_connected.get(family)
            if connected is not True:
                available = False
                reason = "cli_not_verified" if connected is None else "cli_not_connected"
        elif family == "local":
            model = route_id.split(".", 1)[1] if "." in route_id else route_id
            # Registry routes commonly prefix the transport as `ollama.`.
            if model.startswith("ollama."):
                model = model[len("ollama."):]
            if not ollama_reachable:
                available, reason = False, "ollama_unreachable"
            elif model not in local_models:
                available, reason = False, "local_model_missing"
        out[route_id] = RouteAvailability(route_id, family, available, reason)
    return out


def available_route_ids(projection: dict[str, RouteAvailability]) -> set[str]:
    return {route_id for route_id, item in projection.items() if item.available}


def resolve_route_availability(route_ids: list[str]) -> dict[str, RouteAvailability]:
    """Resolve current app state. Fail closed on every uncertain live probe."""
    from errorta_app import provider_keys, settings
    from errorta_app.routes.gateway import _PROBE_CACHE, _provider_configured
    from errorta_model_gateway.providers import async_registry

    async_registry.ensure_bootstrapped()
    keys = provider_keys.load_all()
    configured = {
        cls for cls in async_registry.list_provider_classes()
        if _provider_configured(cls, keys)
    }
    explicit = settings.get_model_family_allowlist()
    enabled = effective_family_allowlist(configured, explicit)
    cli = {
        cls: (entry.get("connected") if isinstance(entry, dict) else None)
        for cls, entry in _PROBE_CACHE.items()
    }
    local_models: set[str] = set()
    reachable = False
    try:
        from errorta_ollama import detect, pull, settings as ollama_settings

        reachable = bool(detect.probe(ollama_settings.load().host).reachable)
        if reachable:
            local_models = set(pull.installed_models())
    except Exception:
        reachable = False
    return project_availability(
        route_ids,
        configured_families=configured,
        enabled_families=enabled,
        cli_connected=cli,
        local_models=local_models,
        ollama_reachable=reachable,
    )


__all__ = [
    "RouteAvailability", "available_route_ids", "effective_family_allowlist",
    "project_availability", "resolve_route_availability",
]
