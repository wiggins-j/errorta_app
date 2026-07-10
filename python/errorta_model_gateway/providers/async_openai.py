"""F034-4 — OpenAI (ChatGPT) async handler.

POST ``https://api.openai.com/v1/chat/completions``. Standard
chat-completions shape with ``messages`` (including ``system``) and a
``Bearer`` Authorization header.

Errors normalized to ``errorta_council.gateway_local.{FatalError,
RetryableError}``.
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

_DEFAULT_BASE_URL = "https://api.openai.com"

# Default known routes. Reasoning models (o1*) deliberately omitted
# from the round-robin default for v0.6 because their response latency
# is dramatically higher; operators can add them via the room editor.
_DEFAULT_ROUTES = [
    RouteDescriptor(route_id="openai.gpt-4o",      label="GPT-4o",      family="gpt-4o"),
    RouteDescriptor(route_id="openai.gpt-4o-mini", label="GPT-4o mini", family="gpt-4o"),
    RouteDescriptor(route_id="openai.o1-mini",    label="o1 mini",     family="o1"),
    RouteDescriptor(route_id="openai.o1-preview", label="o1 preview",  family="o1"),
]


class OpenAIHandler:
    """AsyncProviderHandler for ChatGPT models via api.openai.com."""

    provider_class: str = "openai"
    display_name: str = "OpenAI API"

    def __init__(self, *, base_url: str = _DEFAULT_BASE_URL) -> None:
        self._base_url = base_url.rstrip("/")

    async def call(
        self, request: AsyncProviderRequest, *, api_key: str | None
    ) -> AsyncProviderResult:
        from errorta_council.gateway_local import FatalError, RetryableError

        if not api_key:
            raise FatalError(
                "openai_missing_api_key: configure under Settings → Provider keys"
            )

        body: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
        }
        if request.max_output_tokens is not None:
            # OpenAI accepts either max_tokens or max_completion_tokens
            # depending on the model. max_tokens still works for the
            # gpt-4o family; o1 models require max_completion_tokens.
            # For v0.6 we keep max_tokens and let o1 callers override
            # via AsyncProviderRequest.extra if needed.
            body["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        # Allow extra to override / extend (e.g. response_format).
        for k, v in (request.extra or {}).items():
            body.setdefault(k, v)

        url = f"{self._base_url}/v1/chat/completions"
        headers = {
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                resp = await client.post(url, json=body, headers=headers)
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout):
            raise RetryableError("openai_timeout") from None
        except httpx.ConnectError:
            raise RetryableError("openai_provider_unavailable") from None
        except httpx.HTTPError as exc:
            raise RetryableError(
                f"openai_gateway_error: {type(exc).__name__}"
            ) from None

        duration_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code == 429:
            raise RetryableError(f"openai_rate_limited: {resp.status_code}")
        if 500 <= resp.status_code < 600:
            raise RetryableError(f"openai_provider_5xx: {resp.status_code}")
        if resp.status_code in (401, 403):
            raise FatalError(
                f"openai_auth_failed: {resp.status_code} — check API key"
            )
        if resp.status_code == 404:
            raise FatalError(f"openai_model_not_found: {request.model}")
        if resp.status_code >= 400:
            raise FatalError(f"openai_4xx: {resp.status_code}")

        try:
            data = resp.json()
        except (ValueError, Exception):
            raise FatalError("openai_malformed_response: not_json") from None

        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise FatalError("openai_malformed_response: missing_choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise FatalError("openai_malformed_response: bad_choice_shape")
        message = first.get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content:
            raise FatalError("openai_malformed_response: missing_content")

        usage = data.get("usage") if isinstance(data, dict) else None
        input_tokens = None
        output_tokens = None
        cache_read_input_tokens = None
        if isinstance(usage, dict):
            if isinstance(usage.get("prompt_tokens"), int):
                input_tokens = usage["prompt_tokens"]
            if isinstance(usage.get("completion_tokens"), int):
                output_tokens = usage["completion_tokens"]
            details = usage.get("prompt_tokens_details")
            if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
                cache_read_input_tokens = details["cached_tokens"]

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
        )

    def list_routes(self, *, configured: bool) -> list[RouteDescriptor]:
        return list(_DEFAULT_ROUTES)

    def validate_route(self, route_id: str) -> ValidationResult:
        if not route_id.startswith("openai."):
            return ValidationResult(
                ok=False, reason="route_id must start with 'openai.'"
            )
        model = route_id[len("openai."):]
        if not model:
            return ValidationResult(ok=False, reason="model name is empty")
        return ValidationResult(ok=True)


    async def test_connection(
        self, *, api_key: str | None
    ) -> TestConnectionResult:
        """GET /v1/models — free list-models probe."""
        if not api_key:
            return TestConnectionResult(False, "no API key configured", 0)
        url = f"{self._base_url}/v1/models"
        headers = {"authorization": f"Bearer {api_key}"}
        import time as _time
        start = _time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            return TestConnectionResult(
                False, f"network error: {type(exc).__name__}",
                int((_time.monotonic() - start) * 1000))
        latency = int((_time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            try:
                data = resp.json()
                n = len(data.get("data") or [])
                return TestConnectionResult(
                    True, f"auth OK · {n} models available", latency)
            except Exception:
                return TestConnectionResult(True, "auth OK", latency)
        if resp.status_code in (401, 403):
            return TestConnectionResult(
                False, f"auth failed ({resp.status_code}) — check API key", latency)
        if resp.status_code == 429:
            return TestConnectionResult(False, "rate limited", latency)
        return TestConnectionResult(False, f"HTTP {resp.status_code}", latency)


async_registry.register("openai", OpenAIHandler)


__all__ = ["OpenAIHandler"]
