"""F040 — ChatGPT subscription handler via the official ``codex`` CLI.

Mirror of ``async_claude_cli`` for OpenAI's Codex CLI, which authenticates
against the user's ChatGPT Plus/Pro **subscription**. Verified live against
``codex-cli 0.133`` (the binary bundled in the Codex desktop app, logged in via
ChatGPT).

Headless invocation (constrained to a plain completion):

    codex exec --json --sandbox read-only --skip-git-repo-check -m <model> -

with the prompt on **stdin** (the trailing ``-``). ``--sandbox read-only`` is
the constraint (no writes / no side effects). ``--json`` emits a **JSONL event
stream**, not a single object::

    {"type":"thread.started",...}
    {"type":"turn.started"}
    {"type":"item.completed","item":{"type":"agent_message","text":"<answer>"}}
    {"type":"turn.completed","usage":{"input_tokens":N,"output_tokens":M,...}}

We take the last ``agent_message`` item's text as the answer and the
``turn.completed`` usage for tokens. ``--skip-git-repo-check`` is required so the
CLI runs in our isolated temp cwd.

The ``codex`` binary may not be on PATH (it ships inside ``Codex.app``); we
resolve PATH first, then the known app-bundle location.

Errors normalize to ``errorta_council.gateway_local.{FatalError,
RetryableError}`` (lazy import to avoid a cycle). A ``codex_cli`` member is
classified ``remote`` egress by the engine adapter (the CLI phones home to
OpenAI), so it passes ``verify_payload_route_alignment``.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from . import async_registry
from ._cli_common import (
    classify_test_result,
    cli_version,
    flatten_messages,
    initial_cli_concurrency,
    resolve_cli_binary,
    resolve_cli_binary_detailed,
    run_cli_subprocess,
)
from .async_base import (
    AsyncProviderRequest,
    AsyncProviderResult,
    RouteDescriptor,
    TestConnectionResult,
    ValidationResult,
)

_CODEX_SEMAPHORE = asyncio.Semaphore(initial_cli_concurrency())


def set_codex_concurrency(n: int) -> None:
    """Resize the codex CLI concurrency gate. Call BEFORE a run dispatches so
    subsequent calls see it; in-flight acquisitions are unaffected. F087 Slice 0."""
    global _CODEX_SEMAPHORE
    _CODEX_SEMAPHORE = asyncio.Semaphore(max(1, int(n)))

# The codex CLI binary often isn't on PATH — it ships inside the Codex app.
_APP_BUNDLE_BINARY = "/Applications/Codex.app/Contents/Resources/codex"

# A ChatGPT-account Codex rejects explicit `-m <id>` ("model is not supported
# when using Codex with a ChatGPT account") and uses the account's own default
# model. So the primary route is `codex_cli.default` (no `-m`). An explicit
# model id is still passable for non-ChatGPT setups (API-key codex).
_DEFAULT_MODEL_SENTINELS = {"", "default", "auto"}
_DEFAULT_ROUTES = [
    RouteDescriptor(
        route_id="codex_cli.default",
        label="ChatGPT Codex (subscription, account default)",
        family="codex",
    ),
]


def resolve_codex_binary() -> str | None:
    """Find the codex binary: PATH/common install dirs, then Codex.app.

    ``ERRORTA_CODEX_CLI=/absolute/path/to/codex`` is an explicit escape hatch
    for users running a packaged Errorta app whose environment cannot see their
    shell PATH.
    """
    return resolve_cli_binary(
        ["codex"],
        env_var="ERRORTA_CODEX_CLI",
        extra_paths=[_APP_BUNDLE_BINARY],
    )


def is_available(*, override_path: str | None = None) -> bool:
    """True when the ``codex`` CLI resolves (PATH or Codex.app bundle).

    Subscription CLIs need no API key — they're "configured" (selectable in
    the room editor) the moment their binary is installed; the CLI itself
    owns the ChatGPT-account OAuth. CHEAP: filesystem resolution only, no
    version probe and no model call. ``override_path`` (F040-01) is honored
    ahead of PATH.
    """
    return resolve_cli_binary(
        ["codex"],
        override_path=override_path,
        env_var="ERRORTA_CODEX_CLI",
        extra_paths=[_APP_BUNDLE_BINARY],
    ) is not None


def _parse_jsonl_events(
    stdout: str,
) -> tuple[str | None, int | None, int | None, int | None, int | None, str | None]:
    """Return (answer_text, input, output, cache_read, cache_write, error).

    The answer is the last ``item.completed`` of type ``agent_message``; usage
    comes from ``turn.completed``. Codex reports failures as ``{"type":"error"}``
    / ``{"type":"turn.failed"}`` events on stdout (exit 1, empty stderr), so we
    surface the error message here.
    """
    answer: str | None = None
    in_tok: int | None = None
    out_tok: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    error: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "item.completed":
            item = ev.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    answer = text  # keep the last agent_message
        elif etype == "turn.completed":
            usage = ev.get("usage")
            if isinstance(usage, dict):
                if isinstance(usage.get("input_tokens"), int):
                    in_tok = usage["input_tokens"]
                if isinstance(usage.get("output_tokens"), int):
                    out_tok = usage["output_tokens"]
                # Cache tokens (D4: detail only). Mirror the anthropic map:
                # cache_creation_input_tokens (provider) → cache_write.
                if isinstance(usage.get("cache_read_input_tokens"), int):
                    cache_read = usage["cache_read_input_tokens"]
                if isinstance(usage.get("cache_creation_input_tokens"), int):
                    cache_write = usage["cache_creation_input_tokens"]
        elif etype in ("error", "turn.failed"):
            msg = ev.get("message")
            if isinstance(ev.get("error"), dict):
                msg = ev["error"].get("message", msg)
            if isinstance(msg, str) and msg:
                error = msg
    return answer, in_tok, out_tok, cache_read, cache_write, error


class CodexCliHandler:
    """AsyncProviderHandler backed by the official ``codex`` CLI subscription."""

    provider_class: str = "codex_cli"
    display_name: str = "Codex CLI"

    def __init__(self, *, binary: str | None = None) -> None:
        self._binary = binary  # None → resolve at call time

    async def call(
        self, request: AsyncProviderRequest, *, api_key: str | None
    ) -> AsyncProviderResult:
        from errorta_council.gateway_local import FatalError, RetryableError

        model = request.model.strip()
        if not model:
            raise FatalError("codex_cli_empty_model")

        binary = self._binary or resolve_codex_binary()
        if not binary:
            raise FatalError(
                "codex_cli_not_installed: the 'codex' CLI is not on PATH or in Codex.app"
            )

        prompt = flatten_messages(request.messages)
        argv = [
            binary, "exec",
            "--json",
            "--sandbox", "read-only",
            "--skip-git-repo-check",
        ]
        # A ChatGPT-account Codex rejects explicit `-m`; only pass it for a
        # non-default model (API-key / other setups).
        if model.lower() not in _DEFAULT_MODEL_SENTINELS:
            argv += ["-m", model]
        argv.append("-")  # prompt on stdin

        start = time.monotonic()
        stdout, stderr, returncode = await run_cli_subprocess(
            argv=argv,
            prompt=prompt,
            timeout_seconds=request.timeout_seconds,
            semaphore=_CODEX_SEMAPHORE,
            error_prefix="codex_cli",
            cwd_prefix="errorta-codex-cli-",
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        content, in_tok, out_tok, cache_read, cache_write, error_msg = (
            _parse_jsonl_events(stdout)
        )

        # Codex reports failures as JSONL error/turn.failed events (exit 1,
        # empty stderr). Combine both signals for classification.
        combined = f"{stderr}\n{error_msg or ''}".lower()
        if (returncode != 0 or error_msg) and not content:
            if any(
                t in combined
                for t in (
                    "log in",
                    "login",
                    "not authenticated",
                    "unauthorized",
                    "401",
                )
            ):
                raise FatalError(
                    "codex_cli_not_authenticated: run 'codex login' with your subscription"
                )
            if (
                ("rate" in combined and "limit" in combined)
                or "usage limit" in combined
                or "429" in combined
            ):
                raise RetryableError(
                    f"codex_cli_rate_limited: {(error_msg or stderr)[:160]}"
                )
            # Tightened (review #4): only a genuine model-rejection is fatal —
            # NOT any error that merely mentions "model" + "not" (e.g. a
            # transient "could not reach model endpoint"), which must retry.
            if "not supported" in combined or "invalid_request" in combined:
                raise FatalError(
                    f"codex_cli_model_rejected: {(error_msg or stderr)[:200]} "
                    "(ChatGPT-account Codex uses its own default model — use "
                    "route 'codex_cli.default')"
                )
            raise FatalError(
                f"codex_cli_failed: exit {returncode}: {(error_msg or stderr)[:200]}"
            )

        if not content or not content.strip():
            raise FatalError("codex_cli_empty_result")

        return AsyncProviderResult(
            content=content,
            provider_class=self.provider_class,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            duration_ms=duration_ms,
            raw_usage_available=(in_tok is not None and out_tok is not None),
            cache_read_input_tokens=cache_read,
            cache_write_input_tokens=cache_write,
        )

    def resolve_details(self, *, override_path: str | None = None) -> dict[str, Any]:
        """Cheap detect (NO billable model call): binary provenance + version."""
        details = resolve_cli_binary_detailed(
            ["codex"],
            override_path=override_path,
            env_var="ERRORTA_CODEX_CLI",
            extra_paths=[_APP_BUNDLE_BINARY],
        )
        if details is None:
            return {
                "provider": self.provider_class,
                "state": "not_installed",
                "found": False,
                "path": "",
                "name_used": "",
                "source": "",
                "version": "",
                "login": "",
                "detail": "",
            }
        return {
            "provider": self.provider_class,
            "state": "installed",
            "found": True,
            "path": details["path"],
            "name_used": details["name_used"],
            "source": details["source"],
            "version": cli_version(details["path"]) or "",
            "login": "",
            "detail": "",
        }

    async def probe_auth(self) -> dict[str, Any]:
        """Live auth classification — EXPENSIVE (a real model call).

        Only the explicit Test route calls this; never the cheap detect path.
        """
        result = await self.test_connection(api_key=None)
        return classify_test_result(result)

    def list_routes(self, *, configured: bool) -> list[RouteDescriptor]:
        return list(_DEFAULT_ROUTES)

    def validate_route(self, route_id: str) -> ValidationResult:
        if not route_id.startswith("codex_cli."):
            return ValidationResult(ok=False, reason="route_id must start with 'codex_cli.'")
        if not route_id[len("codex_cli."):]:
            return ValidationResult(ok=False, reason="model name is empty")
        return ValidationResult(ok=True)

    async def test_connection(self, *, api_key: str | None) -> TestConnectionResult:
        from errorta_council.gateway_local import FatalError, RetryableError

        start = time.monotonic()
        try:
            result = await self.call(
                AsyncProviderRequest(
                    model="default",
                    messages=[{"role": "user", "content": "Reply with the single word: ok"}],
                    max_output_tokens=16, timeout_seconds=90,
                ),
                api_key=None,
            )
        except FatalError as exc:
            latency = int((time.monotonic() - start) * 1000)
            detail = str(exc)
            if "not_installed" in detail:
                return TestConnectionResult(False, "codex CLI not installed", latency)
            if "not_authenticated" in detail:
                return TestConnectionResult(False, "codex CLI not logged in", latency)
            return TestConnectionResult(False, detail[:120], latency)
        except RetryableError as exc:
            latency = int((time.monotonic() - start) * 1000)
            return TestConnectionResult(False, str(exc)[:120], latency)
        latency = int((time.monotonic() - start) * 1000)
        ok = bool(result.content)
        return TestConnectionResult(ok, "subscription CLI ready" if ok else "no response", latency)


async_registry.register("codex_cli", CodexCliHandler)


__all__ = ["CodexCliHandler", "resolve_codex_binary"]
