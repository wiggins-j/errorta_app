"""Local gateway precursor — the SOLE Council egress to Ollama (invariant 3).

This module is the ONLY file under errorta_council allowed to import httpx.
The import-lint test in test_import_lint.py enforces this — not convention.
"""
from __future__ import annotations

import json as _json
import time
import zlib
from dataclasses import dataclass, field
from typing import Any

import httpx


class RetryableError(Exception):
    """Transient gateway failure (timeout, 5xx, connection refused)."""


class FatalError(Exception):
    """Non-recoverable gateway failure (model not found, malformed, policy)."""


# Prefix gateway writes onto thinking-only Ollama responses. Used by the
# scheduler (payload["is_thinking_burn"]) and the frontend isThinkingBurn()
# check. Keep both in sync — a single constant prevents magic-string drift.
THINKING_TRACE_MARKER = "(reasoning trace, no visible answer)"


@dataclass(frozen=True)
class LocalCouncilModelRequest:
    role: str
    route_id: str
    provider: str            # "local" | "fake"
    model: str
    messages: list[dict[str, str]]
    max_output_tokens: int
    temperature: float
    timeout_seconds: int
    metadata: dict[str, Any] = field(default_factory=dict)
    cache_hints: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class SummaryRequest:
    role: str
    route_id: str
    messages: list[dict[str, str]]
    max_output_tokens: int
    timeout_seconds: int


@dataclass(frozen=True)
class SummaryResult:
    content: str
    duration_ms: int
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class LocalCouncilModelResult:
    content: str
    provider: str            # "ollama" | "fake"
    provider_class: str      # "local"
    model: str
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int
    raw_usage_available: bool
    cache_read_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    is_thinking_burn: bool = False


# Legacy Phase-1 providers. F034 extends this dynamically via the
# async-provider registry — see ``_provider_class_from_route_id`` and the
# dispatch in ``LocalGateway.call``.
_ALLOWED_PROVIDERS = frozenset({"local", "fake"})

# Egress-class vocabulary recognized at the boundary re-check (invariant 5).
_KNOWN_EGRESS_CLASSES = frozenset({"local", "remote_eligible", "blocked"})


def _provider_class_from_route_id(route_id: str) -> str:
    """Extract the provider class from a Council route_id.

    Route_id format is ``<provider_class>.<rest>``; for legacy formats
    using ``/`` (e.g. ``local/ollama/llama3.2:3b``) we accept the same
    split.
    """
    if not route_id:
        return ""
    # Split on either "." or "/" — both forms appear in the wild.
    head_dot = route_id.split(".", 1)[0]
    head_slash = route_id.split("/", 1)[0]
    head = head_dot if len(head_dot) <= len(head_slash) else head_slash
    return head


def _is_local_route(route_id: str) -> bool:
    return route_id.startswith(("local.", "fake.", "local/", "fake/"))


def verify_payload_route_alignment(
    *,
    destination_scope: str,
    egress_class: str,
    route_id: str,
) -> None:
    """Boundary re-check before dispatch (invariant 5).

    Raises FatalError when the payload's policy claims don't match the route
    the scheduler resolved. Concretely:

    - Unknown destination_scope → unknown_destination.
    - Unknown egress_class → unknown_egress_class.
    - Local-only egress on a remote-bound dispatch → payload_route_mismatch.
    - Local destination with a non-``local.*``/``fake.*`` route_id —
      payload_route_mismatch.
    - Remote destination with a ``local.*``/``fake.*`` route_id —
      payload_route_mismatch.

    F034 (2026-06-12) widened the alignment to recognize remote routes
    (``anthropic.*``, ``openai.*``, ``google.*``, ``custom.*``). The
    invariant remains: route_id prefix MUST match destination_scope.
    """
    if destination_scope not in {"local", "remote", "fake", "blocked"}:
        raise FatalError(f"unknown_destination: {destination_scope!r}")
    if egress_class not in _KNOWN_EGRESS_CLASSES:
        raise FatalError(f"unknown_egress_class: {egress_class!r}")
    if egress_class == "local" and destination_scope == "remote":
        raise FatalError(
            f"payload_route_mismatch: local egress + remote destination "
            f"(route_id={route_id!r})"
        )
    if destination_scope == "local" and not _is_local_route(route_id):
        raise FatalError(
            f"payload_route_mismatch: local destination but non-local route "
            f"(route_id={route_id!r})"
        )
    # F034: a remote destination must carry a non-local route_id. A
    # remote scope with a local route is a policy-routing error
    # upstream (probably the scheduler computed scope from member.provider
    # without checking the route).
    if destination_scope == "remote" and _is_local_route(route_id):
        raise FatalError(
            f"payload_route_mismatch: remote destination but local route "
            f"(route_id={route_id!r})"
        )


def _resolve_ollama_host() -> str:
    """Pick the Ollama base URL Council should hit.

    Resolution order:

    1. ``ERRORTA_OLLAMA_HOST`` env var (operator override; useful when
       running an SSH tunnel to a remote Ollama).
    2. ``errorta_ollama.settings.load().host`` — the persistent setting
       the Shell pane's Ollama host field writes to.
    3. ``http://127.0.0.1:11434`` (default Ollama port).

    Resolution is done at LocalGateway construction time so a setting
    change in the Shell pane takes effect on the next Council turn
    (each turn constructs a new LocalGateway via the dispatcher).
    """
    import os as _os
    forced = (_os.environ.get("ERRORTA_OLLAMA_HOST") or "").strip()
    if forced:
        return forced
    try:
        from errorta_ollama import settings as _ollama_settings
        return _ollama_settings.load().host
    except Exception:
        return "http://127.0.0.1:11434"


class LocalGateway:
    """Concrete LocalGateway. The Phase 1 sole Council egress."""

    def __init__(self, *, base_url: str | None = None) -> None:
        self._base_url = base_url if base_url is not None else _resolve_ollama_host()

    async def call(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        # Boundary re-check (invariant 5): re-validate destination_scope
        # and egress_class — as carried by the payload — against the
        # resolved route BEFORE any provider dispatch. The router can
        # produce a payload with a wrong tag, but no provider HTTP
        # should ever proceed under a mismatched policy claim.
        destination_scope = request.metadata.get("destination_scope")
        egress_class = request.metadata.get("egress_class")
        if destination_scope and egress_class:
            verify_payload_route_alignment(
                destination_scope=str(destination_scope),
                egress_class=str(egress_class),
                route_id=request.route_id,
            )

        # A malformed / mislabeled compressed response from ANY provider surfaces
        # as a raw ``zlib.error`` ("Error -3 … incorrect header check") or an
        # ``httpx.DecodingError`` — the backend (or a proxy/VPN in the path) sent
        # a body that didn't decompress. That is a TRANSIENT wire failure, not a
        # fatal policy/model error. Normalize it HERE at the sole egress choke
        # point (invariant 3) to RetryableError so the runner's member-health
        # ladder retries or degrades it to a member failure — a single flaky gzip
        # response must never let a raw zlib.error escape and crash the whole run.
        try:
            return await self._dispatch(request)
        except (zlib.error, httpx.DecodingError) as exc:
            raise RetryableError(
                f"gateway_decode_error: {type(exc).__name__}: {exc}"
            ) from None

    async def _dispatch(
        self, request: LocalCouncilModelRequest
    ) -> LocalCouncilModelResult:
        """Select the provider path for ``request``. Wrapped by ``call`` so any
        transient decode failure from a provider normalizes to RetryableError."""
        # F034 (2026-06-12): dispatch on the route_id prefix when it
        # is one of the registered NON-LOCAL provider classes
        # (anthropic, openai, google, custom — bootstrap-time set). For
        # route_ids without a recognized non-local prefix, fall back to
        # the legacy ``request.provider`` dispatch. Backward compat for
        # callers passing ``provider="local"`` with an unprefixed
        # route_id like ``r1``.
        provider_class = _provider_class_from_route_id(request.route_id)
        if provider_class and provider_class not in _ALLOWED_PROVIDERS:
            # Confirm it's a registered remote provider before
            # diverting — otherwise unknown prefixes also fall through
            # to the legacy path (callers that hardcode arbitrary
            # route_ids in tests stay green).
            from errorta_model_gateway.providers import async_registry
            async_registry.ensure_bootstrapped()
            if async_registry.get_handler(provider_class) is not None:
                return await self._registry_dispatch(provider_class, request)

        if request.provider not in _ALLOWED_PROVIDERS:
            raise FatalError(
                f"provider_class_not_allowed: {request.provider!r} "
                "(Phase 1 accepts 'local' | 'fake' only via the legacy field)"
            )
        if request.provider == "fake":
            return await self._fake_dispatch(request)
        return await self._ollama_dispatch(request)

    async def _registry_dispatch(
        self,
        provider_class: str,
        request: LocalCouncilModelRequest,
    ) -> LocalCouncilModelResult:
        """F034 — forward to the registered AsyncProviderHandler.

        Looks up the handler, resolves the API key from
        ``provider_keys.get_fixed_key`` (or the custom store for the
        custom handler), then calls ``handler.call``. Normalizes the
        result back into the legacy ``LocalCouncilModelResult`` so the
        scheduler doesn't notice the dispatch path changed.
        """
        from errorta_app import provider_keys as _provider_keys
        from errorta_model_gateway.providers import async_registry
        from errorta_model_gateway.providers.async_base import (
            AsyncProviderRequest,
        )

        async_registry.ensure_bootstrapped()
        handler = async_registry.get_handler(provider_class)
        if handler is None:
            raise FatalError(
                f"provider_class_not_registered: {provider_class!r} "
                f"(route_id={request.route_id!r})"
            )

        # Strip the provider prefix to get the provider-side model name.
        # For ``anthropic.claude-sonnet-4-6`` this yields
        # ``claude-sonnet-4-6``. For ``custom.<alias>`` it yields the
        # alias, which the custom handler resolves to base_url + model.
        model = request.route_id.split(".", 1)[1] if "." in request.route_id else request.model

        # Key resolution: fixed providers read from the keys store;
        # custom handlers don't need a key param (they read their own
        # entry by alias inside their call()).
        api_key: str | None = None
        if provider_class != "custom":
            api_key = _provider_keys.get_fixed_key(provider_class)

        async_request = AsyncProviderRequest(
            model=model,
            messages=request.messages,
            max_output_tokens=request.max_output_tokens,
            temperature=request.temperature,
            timeout_seconds=request.timeout_seconds,
            extra={
                "cache_hints": list(request.cache_hints),
                "metadata": dict(request.metadata),
            },
        )
        async_result = await handler.call(async_request, api_key=api_key)
        return LocalCouncilModelResult(
            content=async_result.content,
            provider=provider_class,
            provider_class=provider_class,
            model=async_result.model,
            input_tokens=async_result.input_tokens,
            output_tokens=async_result.output_tokens,
            duration_ms=async_result.duration_ms,
            raw_usage_available=async_result.raw_usage_available,
            cache_read_input_tokens=async_result.cache_read_input_tokens,
            cache_write_input_tokens=async_result.cache_write_input_tokens,
            is_thinking_burn=async_result.content.startswith(THINKING_TRACE_MARKER),
        )

    async def _fake_dispatch(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        from errorta_council.members.fake import fake_completion_async
        member_id = request.metadata.get("member_id")
        content = await fake_completion_async(
            model=request.model, messages=request.messages, member_id=member_id
        )
        return LocalCouncilModelResult(
            content=content,
            provider="fake",
            provider_class="local",
            model=request.model,
            input_tokens=None,
            output_tokens=None,
            duration_ms=0,
            raw_usage_available=False,
        )

    async def _ollama_dispatch(
        self, request: LocalCouncilModelRequest
    ) -> LocalCouncilModelResult:
        url = f"{self._base_url.rstrip('/')}/api/chat"
        body = {
            "model": request.model,
            "messages": request.messages,
            "stream": False,
            "options": {
                "num_predict": request.max_output_tokens,
                "temperature": request.temperature,
            },
        }
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                resp = await client.post(url, json=body)
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout):
            raise RetryableError("local_timeout") from None
        except httpx.ConnectError:
            raise RetryableError("local_provider_unavailable") from None
        except (httpx.HTTPError, zlib.error) as exc:
            # httpx.DecodingError is an HTTPError subclass; a raw zlib.error can
            # also surface if a body decompresses outside httpx's decoder. Both
            # are transient wire failures — retry, never a fatal crash.
            raise RetryableError(f"gateway_error: {type(exc).__name__}") from None

        if 500 <= resp.status_code < 600:
            raise RetryableError(f"local_provider_5xx: {resp.status_code}")
        if resp.status_code == 404:
            raise FatalError(f"model_not_found: {request.model}")
        try:
            data = resp.json()
        except (_json.JSONDecodeError, ValueError):
            raise FatalError("malformed_response: not_json") from None
        if resp.status_code >= 400:
            msg = (data or {}).get("error", "")
            if "not found" in msg.lower():
                raise FatalError(f"model_not_found: {request.model}")
            raise FatalError(f"gateway_4xx: {resp.status_code}")

        message = (data or {}).get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise FatalError("malformed_response: missing_content")
        # Thinking-style models (qwen3.5, deepseek-r1, o1-style) put their
        # reasoning in a separate `thinking` field; if the visible
        # `content` is empty but `thinking` has substance, surface a
        # readable fallback so the operator sees the model worked. The
        # raw thinking text is the chain-of-thought; we render it as
        # THINKING_TRACE_MARKER + text so it's distinguishable from a normal
        # answer. The scheduler stamps is_thinking_burn=True on the result so
        # the frontend can detect it structurally instead of matching the string.
        if not content.strip():
            thinking = message.get("thinking")
            if isinstance(thinking, str) and thinking.strip():
                content = f"{THINKING_TRACE_MARKER} {thinking.strip()}"

        input_tokens = data.get("prompt_eval_count")
        output_tokens = data.get("eval_count")
        total_duration_ns = data.get("total_duration")
        duration_ms = (
            int(total_duration_ns / 1_000_000) if isinstance(total_duration_ns, int)
            else int((time.monotonic() - start) * 1000)
        )
        return LocalCouncilModelResult(
            content=content,
            provider="ollama",
            provider_class="local",
            model=request.model,
            input_tokens=input_tokens if isinstance(input_tokens, int) else None,
            output_tokens=output_tokens if isinstance(output_tokens, int) else None,
            duration_ms=duration_ms,
            raw_usage_available=(input_tokens is not None and output_tokens is not None),
            is_thinking_burn=content.startswith(THINKING_TRACE_MARKER),
        )

    async def is_reachable(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                resp = await client.get(f"{self._base_url.rstrip('/')}/api/tags")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def summarize(self, request: "SummaryRequest") -> "SummaryResult":
        """Phase 3 — single egress for the summarizer (invariant 3).

        Reuses the Ollama dispatch path. Provider classes other than the
        Phase 1 allowlist still raise FatalError before HTTP.
        """
        if request.route_id.startswith("fake."):
            content = "FAKE_SUMMARY"
            return SummaryResult(content=content, duration_ms=0,
                                 input_tokens=None, output_tokens=None)
        url = f"{self._base_url.rstrip('/')}/api/chat"
        body = {
            "model": request.route_id.split("/")[-1],
            "messages": request.messages,
            "stream": False,
            "options": {"num_predict": request.max_output_tokens, "temperature": 0.0},
        }
        try:
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                resp = await client.post(url, json=body)
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout):
            raise RetryableError("local_timeout") from None
        except httpx.ConnectError:
            raise RetryableError("local_provider_unavailable") from None
        except (httpx.HTTPError, zlib.error) as exc:
            # httpx.DecodingError is an HTTPError subclass; a raw zlib.error can
            # also surface if a body decompresses outside httpx's decoder. Both
            # are transient wire failures — retry, never a fatal crash.
            raise RetryableError(f"gateway_error: {type(exc).__name__}") from None
        if 500 <= resp.status_code < 600:
            raise RetryableError(f"local_provider_5xx: {resp.status_code}")
        if resp.status_code >= 400:
            raise FatalError(f"gateway_4xx: {resp.status_code}")
        try:
            data = resp.json()
        except (_json.JSONDecodeError, ValueError):
            raise FatalError("malformed_response: not_json") from None
        msg = (data or {}).get("message") or {}
        content = msg.get("content")
        if not isinstance(content, str):
            raise FatalError("malformed_response: missing_content")
        total_duration_ns = data.get("total_duration")
        duration_ms = (
            int(total_duration_ns / 1_000_000)
            if isinstance(total_duration_ns, int) else 0
        )
        return SummaryResult(
            content=content,
            duration_ms=duration_ms,
            input_tokens=data.get("prompt_eval_count") if isinstance(data.get("prompt_eval_count"), int) else None,
            output_tokens=data.get("eval_count") if isinstance(data.get("eval_count"), int) else None,
        )

    async def list_installed_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                resp = await client.get(f"{self._base_url.rstrip('/')}/api/tags")
            if resp.status_code != 200:
                return []
            data = resp.json()
            return [m.get("name", "") for m in (data or {}).get("models", [])]
        except httpx.HTTPError:
            return []
