"""F034-6 — Custom provider async handler.

Operator-configurable HTTP endpoint. Route_id form ``custom.<alias>``;
the handler reads the matching entry from
``errorta_app.provider_keys.get_custom_entry`` and dispatches to the
endpoint per the entry's ``api_style``:

- ``openai_chat_completions`` — POST ``{base_url}/chat/completions``
  with the OpenAI v1 shape. Most LM Studio / vLLM / llama.cpp servers
  expose this.
- ``anthropic_messages`` — POST ``{base_url}/messages`` with the
  Anthropic v1 shape.
- ``raw`` — POST ``{base_url}`` with ``{"model": "...", "messages": [...]}``
  verbatim, for endpoints that do their own routing.

Auth header is configurable: ``auth_header`` (default ``Authorization``)
+ ``auth_prefix`` (default ``Bearer ``).
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from errorta_app import provider_keys

from .async_base import (
    AsyncProviderHandler,
    AsyncProviderRequest,
    AsyncProviderResult,
    RouteDescriptor,
    TestConnectionResult,
    ValidationResult,
)
from . import async_registry


def _build_auth_headers(entry: dict[str, Any]) -> dict[str, str]:
    api_key = entry.get("api_key", "")
    if not api_key:
        return {}
    header = entry.get("auth_header") or "Authorization"
    prefix = entry.get("auth_prefix") or ""
    return {header.lower(): f"{prefix}{api_key}"}


def _adapt_anthropic(
    messages: list[dict[str, str]],
) -> tuple[str, list[dict[str, Any]]]:
    """Same split as ``async_anthropic._adapt_messages`` for reuse."""
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
        out.append({"role": "user", "content": content})
    system = "\n\n".join(system_parts) if system_parts else ""
    return system, out


class CustomHandler:
    """AsyncProviderHandler for operator-configured custom endpoints."""

    provider_class: str = "custom"
    display_name: str = "Custom"

    async def call(
        self, request: AsyncProviderRequest, *, api_key: str | None
    ) -> AsyncProviderResult:
        from errorta_council.gateway_local import FatalError, RetryableError

        # ``request.model`` for a custom handler is the alias. The
        # api_key parameter is ignored — keys live with the alias in
        # the provider-keys store. (We accept api_key in the signature
        # for Protocol uniformity.)
        alias = request.model
        entry = provider_keys.get_custom_entry(alias)
        if entry is None:
            raise FatalError(
                f"custom_alias_not_found: {alias!r} — add it under "
                f"Settings → Provider keys"
            )

        base_url = (entry.get("base_url") or "").rstrip("/")
        if not base_url:
            raise FatalError(
                f"custom_alias_missing_base_url: {alias!r}"
            )
        style = entry.get("api_style") or "openai_chat_completions"

        # If the entry has a `model` field, use it for the actual call;
        # otherwise pass the alias itself as the model name (for raw
        # endpoints that don't care).
        wire_model = entry.get("model") or alias

        if style == "openai_chat_completions":
            url = f"{base_url}/chat/completions"
            body: dict[str, Any] = {
                "model": wire_model,
                "messages": request.messages,
            }
            if request.max_output_tokens is not None:
                body["max_tokens"] = request.max_output_tokens
            if request.temperature is not None:
                body["temperature"] = request.temperature
        elif style == "anthropic_messages":
            url = f"{base_url}/messages"
            system_text, anth_messages = _adapt_anthropic(request.messages)
            body = {
                "model": wire_model,
                "messages": anth_messages,
                "max_tokens": request.max_output_tokens or 1024,
            }
            if system_text:
                body["system"] = system_text
            if request.temperature is not None:
                body["temperature"] = request.temperature
        elif style == "raw":
            url = base_url
            body = {
                "model": wire_model,
                "messages": request.messages,
            }
            if request.max_output_tokens is not None:
                body["max_tokens"] = request.max_output_tokens
        else:
            raise FatalError(f"custom_unknown_api_style: {style!r}")

        headers = {"content-type": "application/json"}
        headers.update(_build_auth_headers(entry))
        # Anthropic-style endpoints often want this even on local servers.
        if style == "anthropic_messages":
            headers.setdefault("anthropic-version", "2023-06-01")

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                resp = await client.post(url, json=body, headers=headers)
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout):
            raise RetryableError(f"custom_timeout: {alias!r}") from None
        except httpx.ConnectError:
            raise RetryableError(f"custom_provider_unavailable: {alias!r}") from None
        except httpx.HTTPError as exc:
            raise RetryableError(
                f"custom_gateway_error: {type(exc).__name__}"
            ) from None

        duration_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code == 429:
            raise RetryableError(f"custom_rate_limited: {resp.status_code}")
        if 500 <= resp.status_code < 600:
            raise RetryableError(f"custom_provider_5xx: {resp.status_code}")
        if resp.status_code in (401, 403):
            raise FatalError(
                f"custom_auth_failed: {resp.status_code} — check API key for {alias!r}"
            )
        if resp.status_code == 404:
            raise FatalError(f"custom_model_not_found: {wire_model!r}")
        if resp.status_code >= 400:
            raise FatalError(f"custom_4xx: {resp.status_code}")

        try:
            data = resp.json()
        except (ValueError, Exception):
            raise FatalError("custom_malformed_response: not_json") from None

        # Parse response per style. Best-effort — operators may point at
        # endpoints with slightly different shapes; we accept either the
        # OpenAI or Anthropic response forms regardless of request style
        # since some endpoints intermix them.
        content, input_tokens, output_tokens = _parse_response(data)
        if not content:
            raise FatalError("custom_malformed_response: empty_text")

        return AsyncProviderResult(
            content=content,
            provider_class=self.provider_class,
            model=wire_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            raw_usage_available=(
                input_tokens is not None and output_tokens is not None
            ),
        )

    def list_routes(self, *, configured: bool) -> list[RouteDescriptor]:
        # Build the route list from the configured custom entries.
        keys = provider_keys.load_all()
        out: list[RouteDescriptor] = []
        for entry in keys.get("custom") or []:
            alias = entry.get("alias")
            if not alias:
                continue
            label = entry.get("model") or alias
            out.append(RouteDescriptor(
                route_id=f"custom.{alias}",
                label=f"Custom: {label}",
                family="custom",
            ))
        return out

    def validate_route(self, route_id: str) -> ValidationResult:
        if not route_id.startswith("custom."):
            return ValidationResult(
                ok=False, reason="route_id must start with 'custom.'"
            )
        alias = route_id[len("custom."):]
        if not alias:
            return ValidationResult(ok=False, reason="alias is empty")
        entry = provider_keys.get_custom_entry(alias)
        if entry is None:
            return ValidationResult(
                ok=False, reason=f"no custom entry with alias {alias!r}"
            )
        return ValidationResult(ok=True)

    async def test_connection(self, *, api_key: str | None) -> TestConnectionResult:
        keys = provider_keys.load_all()
        entries = list(keys.get("custom") or [])
        if not entries:
            return TestConnectionResult(False, "no custom providers configured", 0)
        import time as _time
        start = _time.monotonic()
        passes = 0
        details = []
        for entry in entries:
            alias = entry.get("alias", "?")
            r = await self._test_one(entry)
            details.append(f"{alias}: {'ok' if r.ok else r.detail}")
            if r.ok:
                passes += 1
        latency = int((_time.monotonic() - start) * 1000)
        ok = passes == len(entries)
        return TestConnectionResult(ok, " · ".join(details), latency)

    async def test_alias(self, alias: str) -> TestConnectionResult:
        entry = provider_keys.get_custom_entry(alias)
        if entry is None:
            return TestConnectionResult(False, f"alias {alias!r} not found", 0)
        return await self._test_one(entry)

    async def _test_one(self, entry) -> TestConnectionResult:
        import time as _time
        base_url = (entry.get("base_url") or "").rstrip("/")
        if not base_url:
            return TestConnectionResult(False, "missing base_url", 0)
        headers = {}
        api_key = entry.get("api_key", "")
        if api_key:
            ah = entry.get("auth_header") or "Authorization"
            ap = entry.get("auth_prefix") or ""
            headers[ah.lower()] = f"{ap}{api_key}"
        start = _time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{base_url}/models", headers=headers)
        except httpx.HTTPError as exc:
            return TestConnectionResult(
                False, f"network: {type(exc).__name__} (url={base_url})",
                int((_time.monotonic() - start) * 1000))
        latency = int((_time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            try:
                data = resp.json()
                items = data.get("data") or data.get("models") or []
                return TestConnectionResult(
                    True, f"{base_url} reachable · {len(items)} models", latency)
            except Exception:
                return TestConnectionResult(True, f"{base_url} reachable", latency)
        if resp.status_code in (401, 403):
            return TestConnectionResult(
                False, f"auth failed ({resp.status_code}) — check key/header",
                latency)
        return TestConnectionResult(
            False, f"HTTP {resp.status_code} from {base_url}/models", latency)


def _parse_response(data: Any) -> tuple[str, int | None, int | None]:
    """Best-effort parse for OpenAI- or Anthropic-shaped responses.

    Returns ``(content, input_tokens, output_tokens)`` with None for
    missing token fields.
    """
    if not isinstance(data, dict):
        return "", None, None
    # Try OpenAI shape first.
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message") or {}
            text = message.get("content")
            if isinstance(text, str) and text:
                usage = data.get("usage") or {}
                input_t = usage.get("prompt_tokens") if isinstance(usage, dict) else None
                output_t = usage.get("completion_tokens") if isinstance(usage, dict) else None
                return (
                    text,
                    input_t if isinstance(input_t, int) else None,
                    output_t if isinstance(output_t, int) else None,
                )
    # Anthropic shape.
    content_blocks = data.get("content")
    if isinstance(content_blocks, list) and content_blocks:
        text_parts: list[str] = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "")
                if isinstance(t, str):
                    text_parts.append(t)
        text = "".join(text_parts)
        if text:
            usage = data.get("usage") or {}
            input_t = usage.get("input_tokens") if isinstance(usage, dict) else None
            output_t = usage.get("output_tokens") if isinstance(usage, dict) else None
            return (
                text,
                input_t if isinstance(input_t, int) else None,
                output_t if isinstance(output_t, int) else None,
            )
    # Last-ditch — raw {"text": "..."} shape.
    text = data.get("text")
    if isinstance(text, str) and text:
        return text, None, None
    return "", None, None


async_registry.register("custom", CustomHandler)


__all__ = ["CustomHandler"]
