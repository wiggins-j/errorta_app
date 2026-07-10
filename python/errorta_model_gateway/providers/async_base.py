"""F034 — Async provider handler interface.

The F030 ``providers/base.py`` defines a SYNC ``Provider`` Protocol used
by the F001 judge path (which historically went through Ollama with
sync httpx). F031 Council is async-first, and F034 extends the model
gateway to call Anthropic / OpenAI / Google / custom HTTP endpoints —
async by necessity.

Every async handler implements ``AsyncProviderHandler``. Adding a new
provider is a new module + one line in
``errorta_model_gateway.providers.async_registry.PROVIDER_HANDLERS``.

Errors are normalized to the two-class hierarchy already established by
``errorta_council.gateway_local``:

- ``FatalError``: invalid request, auth failure, 4xx (except 429),
  model_not_found, malformed response. Won't recover this run.
- ``RetryableError``: timeout, connection error, 429, 5xx. May recover
  on the next turn.

QA P2 #5 (2026-06-12) hardened ``errorta_council.context.transforms.pipeline``
to catch both ``errorta_briefs.connector`` AND ``errorta_council.gateway_local``
error families plus ``SummarizerUnavailable``. F034 handlers raise the
council variants so the existing TransformPipeline catch block routes
them correctly without any change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class AsyncProviderRequest:
    """Input to ``AsyncProviderHandler.call``.

    F034-1 keeps this minimal — additions (tool use, multimodal inputs,
    streaming) are explicit follow-ups. Field names mirror the existing
    sync ``ProviderRequest`` so an operator who knows F030 can read F034
    code without context-switching.
    """

    model: str
    """The provider-side model identifier, with the ``provider_class.``
    prefix stripped. For ``anthropic.claude-sonnet-4-6`` this is
    ``claude-sonnet-4-6``. For ``custom.<alias>`` this is the alias
    itself; the handler resolves it to a model + base_url via the
    provider-keys store.
    """

    messages: list[dict[str, str]]
    """Standard role/content list: ``[{"role": "system"|"user"|"assistant",
    "content": "..."}]``. The handler is responsible for adapting to
    provider-specific message shapes (Anthropic separates ``system``
    from ``messages``; Google uses ``contents`` with a different
    ``role`` enum).
    """

    max_output_tokens: int | None = None
    temperature: float | None = None
    timeout_seconds: int = 30

    extra: dict[str, Any] = field(default_factory=dict)
    """Provider-specific options the handler may consume. Council does
    NOT route untrusted user payload bytes here — anything sensitive
    belongs in ``messages`` so it flows through the RedactionPipeline.
    """


@dataclass(frozen=True)
class AsyncProviderResult:
    """Result of ``AsyncProviderHandler.call``.

    Token counts are normalized: every handler must populate
    ``input_tokens`` and ``output_tokens`` when the provider reports
    them. ``None`` means "the provider didn't tell us"; ``0`` means
    "the provider explicitly reported zero". The distinction matters
    for budget settle paths.
    """

    content: str
    provider_class: str
    """The handler's own class identifier — ``"anthropic"``, etc."""

    model: str
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int
    raw_usage_available: bool = False
    """``True`` iff input_tokens AND output_tokens were both populated
    from the provider's response (vs estimated client-side).
    """
    cache_read_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None


@dataclass(frozen=True)
class RouteDescriptor:
    """Result of ``AsyncProviderHandler.list_routes``.

    Used by the F033 room editor to populate the model dropdown for a
    given provider. The ``route_id`` is the operator-facing string;
    ``label`` is the human display name; ``family`` groups related
    routes in the dropdown.
    """

    route_id: str
    label: str
    family: str | None = None


@dataclass(frozen=True)
class TestConnectionResult:
    """Result of ``AsyncProviderHandler.test_connection``.

    UI surfaces ``ok`` as a green/red badge and ``detail`` as hover text
    on failure. ``latency_ms`` is round-trip wall time including
    response parsing.
    """

    ok: bool
    detail: str
    latency_ms: int


@dataclass(frozen=True)
class ValidationResult:
    """Result of ``AsyncProviderHandler.validate_route``.

    Handlers are NOT required to validate model names — providers can
    silently 404 at HTTP time. Validation here is for client-side
    sanity checks (format, length). ``ok=False`` with a non-None
    ``reason`` surfaces in the room editor.
    """

    ok: bool
    reason: str | None = None


class AsyncProviderHandler(Protocol):
    """The F034 provider contract.

    Implementations live in ``errorta_model_gateway/providers/async_*.py``
    and are registered in ``async_registry.PROVIDER_HANDLERS``. The
    Council gateway dispatcher (``errorta_council.gateway_local``)
    parses ``route_id`` of the form ``<provider_class>.<model>``,
    looks up the handler by ``provider_class``, strips the prefix from
    the model, and calls ``handler.call(request)``.

    Adding a new provider is a single new file + a one-line registry
    edit. No core code changes.
    """

    provider_class: str
    """Stable identifier, e.g. ``"anthropic"``, ``"openai"``, ``"google"``,
    ``"local"``, ``"custom"``. Must match the prefix used in route_ids.
    """

    display_name: str
    """Human-readable label for the Settings UI."""

    async def call(
        self, request: AsyncProviderRequest, *, api_key: str | None
    ) -> AsyncProviderResult:
        """Issue one provider API call and return the normalized result.

        On any HTTP error, raise ``errorta_council.gateway_local.FatalError``
        or ``RetryableError`` per the error-class doc at the top of this
        module.
        """
        ...

    def list_routes(self, *, configured: bool) -> list[RouteDescriptor]:
        """Return the routes this handler exposes.

        ``configured=True`` indicates the provider has a key on file.
        Handlers MAY return the full route catalog regardless (the UI
        renders all routes but greys out un-configured ones); the flag
        is provided so a handler can return a smaller "preferred" list
        when keys are present.
        """
        ...

    def validate_route(self, route_id: str) -> ValidationResult:
        """Cheap client-side sanity check of an operator-edited route_id.

        Handlers SHOULD accept any well-formed string — provider 404s
        surface at call time. Reserved for catching obvious typos
        (wrong provider prefix, empty model name).
        """
        ...

    async def test_connection(
        self, *, api_key: str | None
    ) -> TestConnectionResult:
        """Make the cheapest possible API call to verify the key works.

        UI surfaces the result as a green/red badge with hover-text
        showing ``detail``. Implementations choose the cheapest
        endpoint for their provider (typically a list-models call for
        OpenAI / Google; a 1-token messages call for Anthropic; a
        ``/api/tags`` for Ollama). Returns immediately on auth
        failure with ``ok=False``.
        """
        ...
