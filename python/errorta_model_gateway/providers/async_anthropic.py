"""F034-3 — Anthropic (Claude) async handler.

POST ``https://api.anthropic.com/v1/messages``. Headers per the
Anthropic API spec: ``x-api-key``, ``anthropic-version: 2023-06-01``,
``Content-Type: application/json``.

Anthropic's request shape separates ``system`` from ``messages``. We
adapt the generic ``role: system | user | assistant`` list by hoisting
the first system message into the top-level ``system`` field; any
subsequent system messages are concatenated into the same field with
``\n\n`` separators (matches Anthropic's documented behavior).

Errors are normalized to ``errorta_council.gateway_local.{FatalError,
RetryableError}`` so the existing TransformPipeline catch (QA P2 #5)
routes them. Default route catalog is hard-coded for v0.6; operators
can edit member route_ids freely and the handler passes the model name
through verbatim — provider 404s surface as FatalError.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from .async_base import (
    AsyncProviderHandler,
    AsyncProviderRequest,
    AsyncProviderResult,
    RouteDescriptor,
    TestConnectionResult,
    ValidationResult,
)
from . import async_registry

# Anthropic-side endpoint constants.
_DEFAULT_BASE_URL = "https://api.anthropic.com"
_API_VERSION = "2023-06-01"

# Default known routes for the v0.6 demo path. The operator can address
# any model name via the room editor; this is a starting catalog.
_DEFAULT_ROUTES = [
    RouteDescriptor(route_id="anthropic.claude-opus-4-7",   label="Claude Opus 4.7",   family="opus"),
    RouteDescriptor(route_id="anthropic.claude-opus-4-8",   label="Claude Opus 4.8",   family="opus"),
    RouteDescriptor(route_id="anthropic.claude-sonnet-4-6", label="Claude Sonnet 4.6", family="sonnet"),
    RouteDescriptor(route_id="anthropic.claude-haiku-4-5",  label="Claude Haiku 4.5",  family="haiku"),
    RouteDescriptor(route_id="anthropic.claude-fable-5",    label="Claude Fable 5",    family="fable"),
]


def _adapt_messages(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, Any]]]:
    """Split system messages from user/assistant turns.

    Returns ``(system_text, anthropic_messages)``. The Anthropic
    request shape requires:

    - At least one user message (we don't enforce; provider will 400).
    - System hoisted to a top-level ``system`` string (NOT in the
      messages list).
    - Each remaining message has role ``user`` or ``assistant`` only.
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = (msg.get("role") or "").lower()
        content = msg.get("content") or ""
        if role == "system":
            if content:
                system_parts.append(content)
            continue
        if role in ("user", "assistant"):
            out.append({"role": role, "content": content})
            continue
        # Unknown role: pass through as user so we don't drop content
        # silently — provider will 400 if it's truly invalid.
        out.append({"role": "user", "content": content})
    system = "\n\n".join(system_parts) if system_parts else ""
    return system, out


class AnthropicHandler:
    """AsyncProviderHandler for Claude models via api.anthropic.com."""

    provider_class: str = "anthropic"
    display_name: str = "Anthropic API"

    def __init__(self, *, base_url: str = _DEFAULT_BASE_URL) -> None:
        self._base_url = base_url.rstrip("/")

    async def call(
        self, request: AsyncProviderRequest, *, api_key: str | None
    ) -> AsyncProviderResult:
        from errorta_council.gateway_local import FatalError, RetryableError

        if not api_key:
            raise FatalError(
                "anthropic_missing_api_key: configure under Settings → Provider keys"
            )

        system_text, anth_messages = _adapt_messages(request.messages)
        body: dict[str, Any] = {
            "model": request.model,
            "messages": anth_messages,
            "max_tokens": request.max_output_tokens or 1024,
        }
        cache_hints = list((request.extra or {}).get("cache_hints") or [])
        if system_text:
            if cache_hints:
                body["system"] = [
                    {
                        "type": "text",
                        "text": system_text,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                body["system"] = system_text
        if request.temperature is not None:
            body["temperature"] = request.temperature

        url = f"{self._base_url}/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                resp = await client.post(url, json=body, headers=headers)
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout):
            raise RetryableError("anthropic_timeout") from None
        except httpx.ConnectError:
            raise RetryableError("anthropic_provider_unavailable") from None
        except httpx.HTTPError as exc:
            raise RetryableError(
                f"anthropic_gateway_error: {type(exc).__name__}"
            ) from None

        duration_ms = int((time.monotonic() - start) * 1000)

        # 429 = retry. 5xx = retry. 4xx = fatal.
        if resp.status_code == 429:
            raise RetryableError(f"anthropic_rate_limited: {resp.status_code}")
        if 500 <= resp.status_code < 600:
            raise RetryableError(f"anthropic_provider_5xx: {resp.status_code}")
        if resp.status_code == 401 or resp.status_code == 403:
            raise FatalError(
                f"anthropic_auth_failed: {resp.status_code} — check API key"
            )
        if resp.status_code == 404:
            raise FatalError(f"anthropic_model_not_found: {request.model}")
        if resp.status_code >= 400:
            raise FatalError(f"anthropic_4xx: {resp.status_code}")

        try:
            data = resp.json()
        except (ValueError, Exception):
            raise FatalError("anthropic_malformed_response: not_json") from None

        # Anthropic response shape: {"content": [{"type":"text","text":"..."}], "usage": {...}}
        content_blocks = data.get("content") if isinstance(data, dict) else None
        if not isinstance(content_blocks, list) or not content_blocks:
            raise FatalError("anthropic_malformed_response: missing_content")
        # Concatenate text blocks.
        text_parts: list[str] = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "")
                if isinstance(t, str):
                    text_parts.append(t)
        content = "".join(text_parts)
        if not content:
            raise FatalError("anthropic_malformed_response: empty_text")

        usage = data.get("usage") if isinstance(data, dict) else None
        input_tokens = None
        output_tokens = None
        cache_read_input_tokens = None
        cache_write_input_tokens = None
        if isinstance(usage, dict):
            if isinstance(usage.get("input_tokens"), int):
                input_tokens = usage["input_tokens"]
            if isinstance(usage.get("output_tokens"), int):
                output_tokens = usage["output_tokens"]
            if isinstance(usage.get("cache_read_input_tokens"), int):
                cache_read_input_tokens = usage["cache_read_input_tokens"]
            if isinstance(usage.get("cache_creation_input_tokens"), int):
                cache_write_input_tokens = usage["cache_creation_input_tokens"]

        return AsyncProviderResult(
            content=content,
            provider_class=self.provider_class,
            model=request.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            raw_usage_available=(
                input_tokens is not None and output_tokens is not None
            ),
            cache_read_input_tokens=cache_read_input_tokens,
            cache_write_input_tokens=cache_write_input_tokens,
        )

    def list_routes(self, *, configured: bool) -> list[RouteDescriptor]:
        return list(_DEFAULT_ROUTES)

    def validate_route(self, route_id: str) -> ValidationResult:
        if not route_id.startswith("anthropic."):
            return ValidationResult(
                ok=False, reason="route_id must start with 'anthropic.'"
            )
        model = route_id[len("anthropic."):]
        if not model:
            return ValidationResult(ok=False, reason="model name is empty")
        return ValidationResult(ok=True)

    async def test_connection(
        self, *, api_key: str | None
    ) -> TestConnectionResult:
        """1-token Haiku messages probe — cheapest Anthropic auth check."""
        if not api_key:
            return TestConnectionResult(False, "no API key configured", 0)
        url = f"{self._base_url}/v1/messages"
        # Use the dated Haiku ID — the bare 'claude-haiku-4-5' returns
        # 400 from the API because Anthropic requires the full version
        # string. Haiku is the cheapest variant.
        body = {"model": "claude-haiku-4-5-20251001", "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}]}
        headers = {"x-api-key": api_key,
                   "anthropic-version": _API_VERSION,
                   "content-type": "application/json"}
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            return TestConnectionResult(
                False, f"network error: {type(exc).__name__}",
                int((time.monotonic() - start) * 1000))
        latency = int((time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            return TestConnectionResult(True, "auth + reachability OK", latency)
        if resp.status_code in (401, 403):
            return TestConnectionResult(False, f"auth failed ({resp.status_code}) — check API key", latency)
        if resp.status_code == 429:
            return TestConnectionResult(False, "rate limited (key may be valid)", latency)
        return TestConnectionResult(False, f"HTTP {resp.status_code}", latency)


# Register at module-import time so the registry picks us up
# automatically when ensure_bootstrapped() runs.
async_registry.register("anthropic", AnthropicHandler)


__all__ = ["AnthropicHandler"]
