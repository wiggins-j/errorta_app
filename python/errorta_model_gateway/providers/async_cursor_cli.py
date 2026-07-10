"""Cursor subscription/API-key handler via the official Cursor CLI.

Cursor's current docs use ``agent`` as the CLI binary; older installs and many
automation examples use ``cursor-agent``; some app-bundle installs expose
``cursor agent``. This handler resolves all three shapes and drives print mode
as a plain model backend:

    agent -p --mode ask --trust --output-format json [--model <model>]

The prompt is supplied on stdin, not argv. Cursor's JSON output may be a single
terminal result object (``{"type":"result", "result":"..."}``) or a simpler
``{"result":"..."}`` object depending on CLI version, so parsing accepts both.

Credentials remain owned by Cursor: users run ``agent login`` / ``cursor-agent
login`` or set ``CURSOR_API_KEY`` themselves. Errorta never stores Cursor
credentials for this provider.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import async_registry
from ._cli_common import (
    classify_test_result,
    cli_version,
    flatten_messages,
    initial_cli_concurrency,
    resolve_cli_binary_detailed,
    run_cli_capture,
    run_cli_subprocess,
)
from .async_base import (
    AsyncProviderRequest,
    AsyncProviderResult,
    RouteDescriptor,
    TestConnectionResult,
    ValidationResult,
)

_CURSOR_SEMAPHORE = asyncio.Semaphore(initial_cli_concurrency())


def set_cursor_concurrency(n: int) -> None:
    """Resize the Cursor CLI concurrency gate before a run dispatches."""
    global _CURSOR_SEMAPHORE
    _CURSOR_SEMAPHORE = asyncio.Semaphore(max(1, int(n)))


@dataclass(frozen=True)
class CursorCommand:
    argv_prefix: list[str]
    display_path: str


def _cursor_app_cli() -> str:
    return "/Applications/Cursor.app/Contents/Resources/app/bin/cursor"


def _candidate_cursor_paths() -> list[str]:
    home = Path.home()
    return [
        str(home / ".local" / "bin" / "agent"),
        str(home / ".local" / "bin" / "cursor-agent"),
        str(home / "bin" / "agent"),
        str(home / "bin" / "cursor-agent"),
        "/opt/homebrew/bin/agent",
        "/opt/homebrew/bin/cursor-agent",
        "/usr/local/bin/agent",
        "/usr/local/bin/cursor-agent",
        _cursor_app_cli(),
    ]


def _command_from_path(path: str) -> CursorCommand:
    """Build a ``CursorCommand`` from a resolved binary path.

    A ``cursor`` launcher needs the two-part ``cursor agent`` invocation; the
    ``agent`` / ``cursor-agent`` binaries are invoked directly.
    """
    if os.path.basename(path) == "cursor":
        return CursorCommand([path, "agent"], path)
    return CursorCommand([path], path)


def resolve_cursor_command_detailed(
    *, override_path: str | None = None
) -> tuple[CursorCommand, str] | None:
    """Resolve the Cursor CLI command + its provenance ``source``.

    Precedence: settings ``override_path`` → ``ERRORTA_CURSOR_CLI`` env →
    PATH/common dirs → Cursor.app bundle. Returns ``(command, source)`` or
    ``None``.
    """
    if override_path and os.path.isfile(override_path) and os.access(override_path, os.X_OK):
        return _command_from_path(override_path), "override_settings"

    override = os.environ.get("ERRORTA_CURSOR_CLI")
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return _command_from_path(override), "override_env"

    details = resolve_cli_binary_detailed(
        ["agent", "cursor-agent"],
        env_var=None,
        extra_paths=_candidate_cursor_paths(),
    )
    if details:
        return _command_from_path(details["path"]), details["source"]
    return None


def resolve_cursor_command() -> CursorCommand | None:
    """Find the Cursor CLI command.

    ``ERRORTA_CURSOR_CLI`` may point at ``agent``, ``cursor-agent``, or
    Cursor's ``cursor`` launcher. If it points at ``cursor``, this returns the
    two-part command ``cursor agent``.
    """
    resolved = resolve_cursor_command_detailed()
    return resolved[0] if resolved else None


def is_available(*, override_path: str | None = None) -> bool:
    """True when a Cursor CLI command resolves.

    Auth is checked by ``test_connection``; installed+logged-out still appears
    selectable so the user can run the test and get the exact login hint. CHEAP:
    filesystem resolution only. ``override_path`` (F040-01) is honored ahead of
    PATH.
    """
    return resolve_cursor_command_detailed(override_path=override_path) is not None


_DEFAULT_MODEL_SENTINELS = {"", "default", "auto"}
# FALLBACK ONLY. `list_routes` prefers the account's LIVE catalog via
# `discover_cursor_routes` (`cursor-agent models`) — Cursor renames/removes model
# ids across CLI releases, so a hardcoded list goes stale and seeds dead routes
# (the gpt-5 / gpt-5-codex removal incident). This curated list is used only when
# live discovery can't run (CLI absent, logged out, timeout, parse failure) so the
# dropdown is never empty. `cursor_cli.default` (account default / `auto`) ALWAYS
# resolves; a stale named route now fails fast as a terminal `model_rejected`.
_DEFAULT_ROUTES = [
    RouteDescriptor(
        route_id="cursor_cli.default",
        label="Cursor Agent (account default)",
        family="cursor",
    ),
    RouteDescriptor(
        route_id="cursor_cli.gpt-5.3-codex", label="Cursor Codex 5.3", family="gpt"
    ),
    RouteDescriptor(
        route_id="cursor_cli.gpt-5.3-codex-high",
        label="Cursor Codex 5.3 High",
        family="gpt",
    ),
    RouteDescriptor(route_id="cursor_cli.gpt-5.2", label="Cursor GPT-5.2", family="gpt"),
    RouteDescriptor(
        route_id="cursor_cli.claude-4.5-sonnet",
        label="Cursor Claude Sonnet 4.5",
        family="claude",
    ),
    RouteDescriptor(
        route_id="cursor_cli.claude-4.5-opus-high",
        label="Cursor Claude Opus 4.5",
        family="claude",
    ),
    RouteDescriptor(
        route_id="cursor_cli.gemini-3.1-pro",
        label="Cursor Gemini 3.1 Pro",
        family="gemini",
    ),
]

# --- live model discovery ---------------------------------------------------
# `cursor-agent models` prints one model per line as `<id> - <Label>` under an
# "Available models" header, with a trailing "Tip:" line. We parse the id/label
# pairs and synthesize a route per model. Discovery is best-effort and cached so
# opening the room editor doesn't re-shell on every render.
_MODEL_LINE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s+-\s+(.+)$")
_MODELS_DISCOVERY_TIMEOUT_SECONDS = 6.0
_MODELS_CACHE_TTL_SECONDS = 300.0
# command display_path -> (monotonic_stamp, routes)
_MODELS_CACHE: dict[str, tuple[float, list[RouteDescriptor]]] = {}


def reset_models_cache() -> None:
    """Drop the discovery cache (test seam / forced refresh)."""
    _MODELS_CACHE.clear()


def _family_for_model(model_id: str) -> str:
    low = model_id.lower()
    if low.startswith("claude") or "opus" in low or "sonnet" in low or "haiku" in low:
        return "claude"
    if low.startswith("gemini"):
        return "gemini"
    if low.startswith("grok"):
        return "grok"
    if low.startswith("gpt") or "codex" in low:
        return "gpt"
    return "cursor"


def parse_cursor_models(text: str) -> list[tuple[str, str]]:
    """Parse `<id> - <Label>` lines from `cursor-agent models` output. Skips the
    header, blanks, and the trailing tip. Order-preserving + de-duplicated."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        m = _MODEL_LINE_RE.match(raw.strip())
        if not m:
            continue
        model_id = m.group(1)
        label = m.group(2).strip()
        if model_id in seen:
            continue
        seen.add(model_id)
        out.append((model_id, label))
    return out


def routes_from_models(models: list[tuple[str, str]]) -> list[RouteDescriptor]:
    """Build the route list from discovered (id, label) pairs. `cursor_cli.default`
    (account default) is always first; `auto`/`default` ids are folded into it."""
    routes = [
        RouteDescriptor(
            route_id="cursor_cli.default",
            label="Cursor Agent (account default)",
            family="cursor",
        )
    ]
    seen = {"cursor_cli.default"}
    for model_id, label in models:
        if model_id.lower() in _DEFAULT_MODEL_SENTINELS:
            continue
        route_id = f"cursor_cli.{model_id}"
        if route_id in seen:
            continue
        seen.add(route_id)
        routes.append(
            RouteDescriptor(
                route_id=route_id,
                label=f"Cursor {label}" if label else route_id,
                family=_family_for_model(model_id),
            )
        )
    return routes


def discover_cursor_routes(
    *, override_path: str | None = None, _now: float | None = None
) -> list[RouteDescriptor] | None:
    """The account's LIVE Cursor model catalog as routes, or ``None`` when it
    can't be determined (CLI absent, logged out, error/timeout, unparseable).

    Cached per resolved command path for ``_MODELS_CACHE_TTL_SECONDS`` so the
    room editor doesn't re-shell on every open. Never raises."""
    resolved = resolve_cursor_command_detailed(override_path=override_path)
    if resolved is None:
        return None
    command = resolved[0]
    key = command.display_path
    now = time.monotonic() if _now is None else _now
    cached = _MODELS_CACHE.get(key)
    if cached is not None and (now - cached[0]) < _MODELS_CACHE_TTL_SECONDS:
        return cached[1]
    text = run_cli_capture(
        command.argv_prefix + ["models"], timeout=_MODELS_DISCOVERY_TIMEOUT_SECONDS
    )
    if not text:
        return None
    models = parse_cursor_models(text)
    if not models:
        return None
    routes = routes_from_models(models)
    _MODELS_CACHE[key] = (now, routes)
    return routes


def _extract_result_json(stdout: str) -> dict[str, Any] | None:
    s = stdout.strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
        if isinstance(obj, dict) and "result" in obj:
            return obj
    except (ValueError, json.JSONDecodeError):
        pass
    for line in reversed(s.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict) and (obj.get("type") == "result" or "result" in obj):
            return obj
    return None


class CursorCliHandler:
    """AsyncProviderHandler backed by Cursor CLI print mode."""

    provider_class: str = "cursor_cli"
    display_name: str = "Cursor CLI"

    def __init__(self, *, command: CursorCommand | None = None) -> None:
        self._command = command

    async def call(
        self, request: AsyncProviderRequest, *, api_key: str | None
    ) -> AsyncProviderResult:
        from errorta_council.gateway_local import FatalError, RetryableError

        model = request.model.strip()
        if not model:
            raise FatalError("cursor_cli_empty_model")

        command = self._command or resolve_cursor_command()
        if command is None:
            raise FatalError(
                "cursor_cli_not_installed: install Cursor CLI ('agent' or 'cursor-agent') "
                "or set ERRORTA_CURSOR_CLI"
            )

        prompt = flatten_messages(request.messages)
        argv = [
            *command.argv_prefix,
            "-p",
            "--mode", "ask",
            "--trust",
            "--output-format", "json",
        ]
        if model.lower() not in _DEFAULT_MODEL_SENTINELS:
            argv += ["--model", model]

        start = time.monotonic()
        stdout, stderr, returncode = await run_cli_subprocess(
            argv=argv,
            prompt=prompt,
            timeout_seconds=request.timeout_seconds,
            semaphore=_CURSOR_SEMAPHORE,
            error_prefix="cursor_cli",
            cwd_prefix="errorta-cursor-cli-",
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        obj = _extract_result_json(stdout)
        msg = ""
        if obj is not None:
            msg = str(obj.get("result") or obj.get("message") or "")

        combined = f"{stderr}\n{msg}".lower()
        if returncode != 0 or (obj is not None and obj.get("is_error")):
            if any(t in combined for t in (
                "authentication required",
                "not authenticated",
                "unauthorized",
                "login",
                "api key",
                "401",
            )):
                raise FatalError(
                    "cursor_cli_not_authenticated: run 'agent login' "
                    "or set CURSOR_API_KEY for Cursor CLI"
                )
            if (
                ("rate" in combined and "limit" in combined)
                or "usage limit" in combined
                or "429" in combined
            ):
                raise RetryableError(f"cursor_cli_rate_limited: {(msg or stderr)[:160]}")
            if (
                "not supported" in combined
                or "invalid model" in combined
                or "cannot use this model" in combined
                or "unknown model" in combined
                or "available models:" in combined
            ):
                raise FatalError(f"cursor_cli_model_rejected: {(msg or stderr)[:200]}")
            raise FatalError(f"cursor_cli_failed: exit {returncode}: {(msg or stderr)[:200]}")

        if obj is None:
            raise FatalError("cursor_cli_unparseable_output")
        content = obj.get("result")
        if not isinstance(content, str) or not content.strip():
            raise FatalError("cursor_cli_empty_result")

        # Defensive `usage` parse (F143-01 Slice B). UNVERIFIED against a live
        # cursor CLI: we could not capture a real `--output-format json` payload
        # in this environment (no cursor auth), so the fixture in
        # tests/test_async_cursor_cli.py (`_synthetic_cursor_result_with_usage`)
        # is SYNTHETIC. If the real result object carries a `usage` dict shaped
        # like claude/codex (see async_claude_cli.py ~255-291 and the Cursor CLI
        # output-format docs at cursor.com/docs/cli/reference/output-format),
        # we read it here; otherwise this silently no-ops and cursor stays
        # `raw_usage_available=False` — byte-identical to the prior behavior, so
        # a wrong schema guess causes zero harm (cursor is estimated downstream).
        # Replace the synthetic fixture with a real captured payload once cursor
        # auth is available.
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
        input_tokens = (
            usage.get("input_tokens")
            if isinstance(usage.get("input_tokens"), int)
            else None
        )
        output_tokens = (
            usage.get("output_tokens")
            if isinstance(usage.get("output_tokens"), int)
            else None
        )
        # Cache tokens (D4: detail only, never headline). Mirror
        # async_claude_cli.py incl. the cache_creation → cache_write name map.
        cache_read_input_tokens = (
            usage.get("cache_read_input_tokens")
            if isinstance(usage.get("cache_read_input_tokens"), int)
            else None
        )
        cache_write_input_tokens = (
            usage.get("cache_creation_input_tokens")
            if isinstance(usage.get("cache_creation_input_tokens"), int)
            else None
        )

        return AsyncProviderResult(
            content=content,
            provider_class=self.provider_class,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            raw_usage_available=(input_tokens is not None and output_tokens is not None),
            cache_read_input_tokens=cache_read_input_tokens,
            cache_write_input_tokens=cache_write_input_tokens,
        )

    def resolve_details(self, *, override_path: str | None = None) -> dict[str, Any]:
        """Cheap detect (NO billable model call): binary provenance + version."""
        resolved = resolve_cursor_command_detailed(override_path=override_path)
        if resolved is None:
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
        command, source = resolved
        path = command.display_path
        return {
            "provider": self.provider_class,
            "state": "installed",
            "found": True,
            "path": path,
            "name_used": os.path.basename(path),
            "source": source,
            "version": cli_version(path) or "",
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
        # Prefer the account's LIVE model catalog so the dropdown reflects what
        # Cursor actually accepts (not a list that silently goes stale). Only
        # shell out when the CLI is present; fall back to the curated defaults on
        # any miss so the dropdown is never empty.
        if configured:
            try:
                discovered = discover_cursor_routes()
            except Exception:  # noqa: BLE001 — discovery is best-effort, never fatal
                discovered = None
            if discovered:
                return discovered
        return list(_DEFAULT_ROUTES)

    def validate_route(self, route_id: str) -> ValidationResult:
        if not route_id.startswith("cursor_cli."):
            return ValidationResult(ok=False, reason="route_id must start with 'cursor_cli.'")
        if not route_id[len("cursor_cli."):]:
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
                    max_output_tokens=16,
                    timeout_seconds=90,
                ),
                api_key=None,
            )
        except FatalError as exc:
            latency = int((time.monotonic() - start) * 1000)
            detail = str(exc)
            if "not_installed" in detail:
                return TestConnectionResult(False, "Cursor CLI not installed", latency)
            if "not_authenticated" in detail:
                return TestConnectionResult(False, "Cursor CLI not logged in", latency)
            return TestConnectionResult(False, detail[:120], latency)
        except RetryableError as exc:
            latency = int((time.monotonic() - start) * 1000)
            return TestConnectionResult(False, str(exc)[:120], latency)
        latency = int((time.monotonic() - start) * 1000)
        ok = bool(result.content)
        return TestConnectionResult(ok, "subscription CLI ready" if ok else "no response", latency)


async_registry.register("cursor_cli", CursorCliHandler)


__all__ = [
    "CursorCliHandler",
    "CursorCommand",
    "resolve_cursor_command",
    "resolve_cursor_command_detailed",
    "set_cursor_concurrency",
]
