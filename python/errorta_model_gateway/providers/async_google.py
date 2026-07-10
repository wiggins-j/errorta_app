"""F034-5 — Google (Gemini) async handler.

POST ``https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=<key>``.

Google's request shape uses ``contents`` with a different role enum
(``user`` / ``model``; no ``system`` role — system instructions go in
a top-level ``systemInstruction`` field). API key travels in the query
string (Google's preferred auth for this endpoint).

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

_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"

_DEFAULT_ROUTES = [
    RouteDescriptor(route_id="google.gemini-1.5-pro",   label="Gemini 1.5 Pro",   family="gemini-1.5"),
    RouteDescriptor(route_id="google.gemini-1.5-flash", label="Gemini 1.5 Flash", family="gemini-1.5"),
    RouteDescriptor(route_id="google.gemini-2.0-flash", label="Gemini 2.0 Flash", family="gemini-2.0"),
]


def _adapt_messages(
    messages: list[dict[str, str]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Split system instructions from contents.

    Returns ``(system_text, gemini_contents)``. Gemini's roles are
    ``user`` and ``model`` (NOT ``assistant``). Any ``system`` role
    gets hoisted to a top-level systemInstruction; ``assistant`` is
    rewritten to ``model``.
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
        gemini_role = "model" if role == "assistant" else "user"
        out.append({
            "role": gemini_role,
            "parts": [{"text": content}],
        })
    system = "\n\n".join(system_parts) if system_parts else None
    return system, out


class GoogleHandler:
    """AsyncProviderHandler for Gemini models via generativelanguage.googleapis.com."""

    provider_class: str = "google"
    display_name: str = "Google API"

    def __init__(self, *, base_url: str = _DEFAULT_BASE_URL) -> None:
        self._base_url = base_url.rstrip("/")

    async def call(
        self, request: AsyncProviderRequest, *, api_key: str | None
    ) -> AsyncProviderResult:
        from errorta_council.gateway_local import FatalError, RetryableError

        if not api_key:
            raise FatalError(
                "google_missing_api_key: configure under Settings → Provider keys"
            )

        system_text, contents = _adapt_messages(request.messages)
        body: dict[str, Any] = {"contents": contents}
        if system_text:
            body["systemInstruction"] = {
                "role": "user", "parts": [{"text": system_text}],
            }
        generation_config: dict[str, Any] = {}
        if request.max_output_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_output_tokens
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        if generation_config:
            body["generationConfig"] = generation_config

        url = (
            f"{self._base_url}/v1beta/models/{request.model}:generateContent"
            f"?key={api_key}"
        )
        headers = {"content-type": "application/json"}

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                resp = await client.post(url, json=body, headers=headers)
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout):
            raise RetryableError("google_timeout") from None
        except httpx.ConnectError:
            raise RetryableError("google_provider_unavailable") from None
        except httpx.HTTPError as exc:
            raise RetryableError(
                f"google_gateway_error: {type(exc).__name__}"
            ) from None

        duration_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code == 429:
            raise RetryableError(f"google_rate_limited: {resp.status_code}")
        if 500 <= resp.status_code < 600:
            raise RetryableError(f"google_provider_5xx: {resp.status_code}")
        if resp.status_code in (401, 403):
            raise FatalError(
                f"google_auth_failed: {resp.status_code} — check API key"
            )
        if resp.status_code == 404:
            raise FatalError(f"google_model_not_found: {request.model}")
        if resp.status_code >= 400:
            raise FatalError(f"google_4xx: {resp.status_code}")

        try:
            data = resp.json()
        except (ValueError, Exception):
            raise FatalError("google_malformed_response: not_json") from None

        # Gemini response shape: {"candidates": [{"content": {"parts": [{"text": "..."}]}}], "usageMetadata": {...}}
        candidates = data.get("candidates") if isinstance(data, dict) else None
        if not isinstance(candidates, list) or not candidates:
            raise FatalError("google_malformed_response: missing_candidates")
        first = candidates[0]
        if not isinstance(first, dict):
            raise FatalError("google_malformed_response: bad_candidate_shape")
        content_block = first.get("content") or {}
        parts = content_block.get("parts") if isinstance(content_block, dict) else None
        if not isinstance(parts, list) or not parts:
            raise FatalError("google_malformed_response: missing_parts")
        text_parts: list[str] = []
        for part in parts:
            if isinstance(part, dict):
                t = part.get("text")
                if isinstance(t, str):
                    text_parts.append(t)
        content = "".join(text_parts)
        if not content:
            raise FatalError("google_malformed_response: empty_text")

        usage = data.get("usageMetadata") if isinstance(data, dict) else None
        input_tokens = None
        output_tokens = None
        if isinstance(usage, dict):
            if isinstance(usage.get("promptTokenCount"), int):
                input_tokens = usage["promptTokenCount"]
            if isinstance(usage.get("candidatesTokenCount"), int):
                output_tokens = usage["candidatesTokenCount"]

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
        )

    def list_routes(self, *, configured: bool) -> list[RouteDescriptor]:
        return list(_DEFAULT_ROUTES)

    def validate_route(self, route_id: str) -> ValidationResult:
        if not route_id.startswith("google."):
            return ValidationResult(
                ok=False, reason="route_id must start with 'google.'"
            )
        model = route_id[len("google."):]
        if not model:
            return ValidationResult(ok=False, reason="model name is empty")
        return ValidationResult(ok=True)


    async def test_connection(
        self, *, api_key: str | None
    ) -> TestConnectionResult:
        """GET /v1beta/models?key=… — free list-models probe."""
        if not api_key:
            return TestConnectionResult(False, "no API key configured", 0)
        url = f"{self._base_url}/v1beta/models?key={api_key}"
        import time as _time
        start = _time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
        except httpx.HTTPError as exc:
            return TestConnectionResult(
                False, f"network error: {type(exc).__name__}",
                int((_time.monotonic() - start) * 1000))
        latency = int((_time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            try:
                data = resp.json()
                n = len(data.get("models") or [])
                return TestConnectionResult(
                    True, f"auth OK · {n} models available", latency)
            except Exception:
                return TestConnectionResult(True, "auth OK", latency)
        if resp.status_code in (401, 403):
            return TestConnectionResult(
                False, f"auth failed ({resp.status_code}) — check API key", latency)
        return TestConnectionResult(False, f"HTTP {resp.status_code}", latency)


async_registry.register("google", GoogleHandler)


__all__ = ["GoogleHandler"]
