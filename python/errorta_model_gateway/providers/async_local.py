"""F034-7 — Local async handler.

Wraps ``errorta_council.gateway_local.LocalGateway`` so the existing
Ollama dispatch is reachable through the new
AsyncProviderHandler registry without changing any of the F031 demo
code paths.

The Council scheduler's dispatcher (see F034-8) prefers this handler
for routes starting with ``local.`` (e.g. ``local.ollama.llama3.2:3b``)
and ``fake.`` (the deterministic test routes used by the demo seed).
"""
from __future__ import annotations

from .async_base import (
    AsyncProviderHandler,
    AsyncProviderRequest,
    AsyncProviderResult,
    RouteDescriptor,
    TestConnectionResult,
    ValidationResult,
)
from . import async_registry

# Default known routes — curated; operators add more via the room
# editor by typing a model name.
_DEFAULT_ROUTES = [
    RouteDescriptor(route_id="local.ollama.llama3.2:3b",  label="Ollama llama3.2:3b", family="ollama"),
    RouteDescriptor(route_id="local.ollama.qwen2.5:7b",   label="Ollama qwen2.5:7b",  family="ollama"),
    RouteDescriptor(route_id="local.ollama.mistral:7b",   label="Ollama mistral:7b",  family="ollama"),
    RouteDescriptor(route_id="local.ollama.gemma2:9b",    label="Ollama gemma2:9b",   family="ollama"),
    # Fake routes — for tests and the demo seed.
    RouteDescriptor(route_id="fake.local.deterministic",  label="Fake (deterministic)", family="fake"),
]


class LocalHandler:
    """AsyncProviderHandler that delegates to the existing LocalGateway."""

    provider_class: str = "local"
    display_name: str = "Local (Ollama)"

    async def call(
        self, request: AsyncProviderRequest, *, api_key: str | None
    ) -> AsyncProviderResult:
        # Lazy import to avoid an import cycle: errorta_council depends
        # on errorta_model_gateway.providers via the dispatcher; this
        # handler depending on errorta_council would close the loop.
        from errorta_council.gateway_local import (
            LocalCouncilModelRequest,
            LocalGateway,
        )

        # The LocalGateway's request needs the legacy fields. Map our
        # generic AsyncProviderRequest in.
        gateway = LocalGateway()
        legacy_req = LocalCouncilModelRequest(
            role="answerer",
            route_id=f"local.ollama.{request.model}",
            messages=request.messages,
            max_output_tokens=request.max_output_tokens or 256,
            temperature=request.temperature or 0.0,
            timeout_seconds=request.timeout_seconds,
            destination_scope="local",
            requested_egress_class="local",
            model=request.model,
            provider_class="local",
        )
        result = await gateway.call(legacy_req)
        return AsyncProviderResult(
            content=result.content,
            provider_class=self.provider_class,
            model=request.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            duration_ms=result.duration_ms,
            raw_usage_available=result.raw_usage_available,
        )

    def list_routes(self, *, configured: bool) -> list[RouteDescriptor]:
        return list(_DEFAULT_ROUTES)

    def validate_route(self, route_id: str) -> ValidationResult:
        if not (route_id.startswith("local.") or route_id.startswith("fake.")):
            return ValidationResult(
                ok=False, reason="route_id must start with 'local.' or 'fake.'"
            )
        return ValidationResult(ok=True)


    async def test_connection(
        self, *, api_key: str | None
    ) -> TestConnectionResult:
        """GET /api/tags from the configured Ollama host.

        Honors the same resolution as LocalGateway —
        ERRORTA_OLLAMA_HOST env var > errorta_ollama.settings.host >
        http://127.0.0.1:11434. So the badge reflects whatever Ollama
        Council actually talks to.
        """
        import time as _time
        import httpx as _httpx
        from errorta_council.gateway_local import _resolve_ollama_host
        host = _resolve_ollama_host().rstrip("/")
        start = _time.monotonic()
        try:
            async with _httpx.AsyncClient(timeout=4) as client:
                resp = await client.get(f"{host}/api/tags")
        except _httpx.HTTPError as exc:
            return TestConnectionResult(
                False, f"network error: {type(exc).__name__} (host={host})",
                int((_time.monotonic() - start) * 1000))
        latency = int((_time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            try:
                data = resp.json()
                models = [m.get("name", "?") for m in (data.get("models") or [])]
                return TestConnectionResult(
                    True, f"{host} reachable · {len(models)} models",
                    latency)
            except Exception:
                return TestConnectionResult(True, f"{host} reachable", latency)
        return TestConnectionResult(
            False, f"HTTP {resp.status_code} from {host}", latency)


async_registry.register("local", LocalHandler)


__all__ = ["LocalHandler"]
