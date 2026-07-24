"""F040 — Claude subscription handler via the official ``claude`` CLI.

Instead of calling ``api.anthropic.com`` with an API key, this handler shells
out to the user's already-logged-in ``claude`` CLI (Claude Code), which
authenticates against their Claude Pro/Max **subscription**. Errorta never sees
or stores the subscription credential — the CLI owns it.

Deliberation invocation (constrained to a plain completion):

    claude -p --tools "" --output-format json --model <model> --max-turns 1

with the prompt on **stdin** (never argv — avoids ARG_MAX and a ``ps``-visible
prompt leak), run in an **isolated empty temp cwd** so the sidecar's
``CLAUDE.md`` / ``.claude`` config can't contaminate the deliberation prompt.
``--tools ""`` (empty allowed-tools) is the load-bearing constraint: no file or
network side effects. ``--max-turns 1`` is belt-and-suspenders.

The CLI prints a single JSON object on success::

    {"type":"result","is_error":false,"result":"<text>",
     "usage":{"input_tokens":N,"output_tokens":M}, "total_cost_usd":...}

``total_cost_usd`` is API-equivalent pricing, NOT the subscription's cost, so we
ignore it; tokens ARE reported, so ``raw_usage_available=True``.

Errors normalize to ``errorta_council.gateway_local.{FatalError,
RetryableError}`` (imported lazily inside methods to avoid an import cycle, like
``async_anthropic``). A subscription-CLI member is classified ``remote`` egress
by the engine adapter (the CLI phones home to Anthropic), so it passes
``verify_payload_route_alignment`` and is treated like any remote provider for
byte-isolation / residency.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
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

_BINARY = "claude"

# Spec 11 (P1a) — read-only in-turn worktree retrieval for DEV turns.
#
# The default deliberation call is single-shot with NO tools (`--tools ""`) in an
# empty temp dir. When the runner threads a task worktree root through
# ``request.extra["metadata"]["dev_repo_read_root"]`` (only the DEV-turn dispatch
# does this, and only when the ``dev_repo_read`` policy is on), we instead run the
# CLI with cwd = that worktree and a READ-ONLY tool allowlist so the model can
# grep/read the rest of the repo before emitting its coding_turn.v1 envelope.
#
# ``--tools`` restricts the AVAILABLE built-in tools (verified against
# ``claude`` CLI 2.0.55: `--tools "Read,Grep,Glob"` exposes EXACTLY
# ``{Read,Grep,Glob}`` in the session-init event — Write/Edit/MultiEdit/Bash/
# NotebookEdit/WebFetch/WebSearch/Task are absent, not merely permission-gated).
# So no write, no exec, and no network tool exists for the model to call; the
# model's real edits still flow only through the coding_turn.v1 envelope +
# execute_dev_turn, never a Write tool. If a future CLI changes ``--tools``
# semantics such that this allowlist can no longer exclude writes/exec, this
# branch MUST NOT ship — fall back to the empty-tools default.
_DEV_REPO_READ_TOOLS = "Read,Grep,Glob"
# Bounded turn budget so the model can do a few read/grep calls before its final
# envelope, without rabbit-holing. The final assistant message (the envelope) is
# still what the parser reads; preceding tool-use turns do not break parsing.
_DEV_REPO_READ_MAX_TURNS = 6

# A GUI .app launched from Finder/Dock inherits a minimal PATH (/usr/bin:/bin:…)
# that excludes the user-level dirs where `claude` is typically installed. So we
# resolve PATH first, then probe the common install locations directly — same
# pattern as codex resolving the Codex.app bundle path. Without this the bundled
# app shows Claude CLI greyed out even though it's installed and logged in.
def _candidate_claude_paths() -> list[str]:
    home = Path.home()
    return [
        str(home / ".local/bin/claude"),
        str(home / ".claude/local/claude"),
        str(home / "bin/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
        "/usr/bin/claude",
    ]


def resolve_claude_binary() -> str | None:
    """Find the ``claude`` binary.

    ``ERRORTA_CLAUDE_CLI=/absolute/path/to/claude`` is an explicit escape hatch
    for packaged Errorta builds launched outside a login shell.
    """
    return resolve_cli_binary(
        [_BINARY],
        env_var="ERRORTA_CLAUDE_CLI",
        extra_paths=_candidate_claude_paths(),
    )


def is_available(*, override_path: str | None = None) -> bool:
    """True when the ``claude`` CLI resolves (PATH or a known install location).

    Subscription CLIs need no API key — they're "configured" (selectable in
    the room editor) the moment their binary is installed; the CLI itself
    owns the subscription OAuth. CHEAP: filesystem resolution only, no version
    probe and no model call. ``override_path`` (F040-01) is the app-supplied
    persisted binary override, honored ahead of PATH.
    """
    return resolve_cli_binary(
        [_BINARY],
        override_path=override_path,
        env_var="ERRORTA_CLAUDE_CLI",
        extra_paths=_candidate_claude_paths(),
    ) is not None

# Heavyweight Node subprocesses + subscription rate limits: cap how many
# ``claude`` invocations run at once across a fan-out round (the cap bounds
# spawned processes — the spawn happens inside this semaphore in the shared
# runner).
_CLAUDE_SEMAPHORE = asyncio.Semaphore(initial_cli_concurrency())


def set_claude_concurrency(n: int) -> None:
    """Resize the claude CLI concurrency gate. Call BEFORE a run dispatches
    (e.g. when a concurrent coding run starts) so subsequent calls see it;
    in-flight acquisitions are unaffected. F087 Slice 0."""
    global _CLAUDE_SEMAPHORE
    _CLAUDE_SEMAPHORE = asyncio.Semaphore(max(1, int(n)))

# CLI-accepted model aliases (the CLI also accepts full dated ids verbatim).
_DEFAULT_ROUTES = [
    RouteDescriptor(
        route_id="claude_cli.opus",
        label="Claude Opus (subscription)",
        family="opus",
    ),
    RouteDescriptor(
        route_id="claude_cli.sonnet",
        label="Claude Sonnet (subscription)",
        family="sonnet",
    ),
    RouteDescriptor(
        route_id="claude_cli.haiku",
        label="Claude Haiku (subscription)",
        family="haiku",
    ),
]


def _dev_repo_read_root(request: AsyncProviderRequest) -> str | None:
    """Spec 11 (P1a): the task worktree root a DEV turn asked us to read, or None.

    Threaded as ``request.extra["metadata"]["dev_repo_read_root"]`` by the runner
    (``gateway_member_caller``), which only sets it on the DEV path when the
    ``dev_repo_read`` policy is on. Returns the path only when it is a non-empty
    string naming an existing directory; every other shape (missing, wrong type,
    empty, nonexistent) yields None so the caller falls back to the single-shot
    empty-temp-dir default — fail safe, never point cwd at a bad/relative path.
    """
    extra = getattr(request, "extra", None)
    if not isinstance(extra, dict):
        return None
    meta = extra.get("metadata")
    if not isinstance(meta, dict):
        return None
    root = meta.get("dev_repo_read_root")
    if not isinstance(root, str) or not root.strip():
        return None
    root = root.strip()
    import os

    if not os.path.isdir(root):
        return None
    return root


def _extract_result_json(stdout: str) -> dict[str, Any] | None:
    """Find the CLI's terminal ``type=="result"`` JSON object.

    ``--output-format json`` emits one object, but tolerate stray leading
    lines: try a straight parse (only accept it if it's the result envelope),
    else scan lines from the end for a well-formed ``type=="result"`` object.
    """
    s = stdout.strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
        if isinstance(obj, dict) and (obj.get("type") == "result" or "is_error" in obj):
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
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    return None


class ClaudeCliHandler:
    """AsyncProviderHandler backed by the official ``claude`` CLI subscription."""

    provider_class: str = "claude_cli"
    display_name: str = "Claude CLI"

    def __init__(self, *, binary: str | None = None) -> None:
        # None → resolve at call time (PATH or a known install location), so the
        # bundled .app — whose PATH lacks ~/.local/bin — still finds claude.
        self._binary = binary

    async def call(
        self, request: AsyncProviderRequest, *, api_key: str | None
    ) -> AsyncProviderResult:
        # api_key is accepted per the Protocol but ignored — the CLI owns the
        # subscription credential.
        from errorta_council.gateway_local import FatalError, RetryableError

        model = request.model.strip()
        if not model:
            raise FatalError("claude_cli_empty_model")

        binary = self._binary or resolve_claude_binary()
        if not binary:
            raise FatalError(
                "claude_cli_not_installed: the 'claude' CLI is not on PATH or in a known location"
            )

        prompt = flatten_messages(request.messages)

        # Spec 11 (P1a): a DEV turn may carry a task worktree root in metadata,
        # requesting read-only in-turn retrieval. Only honor a non-empty string
        # pointing at an existing directory; anything else falls back to the
        # single-shot default (fail safe).
        repo_read_root = _dev_repo_read_root(request)

        if repo_read_root is not None:
            # Read-only retrieval turn: cwd = the worktree, tools = read-only
            # allowlist (Read/Grep/Glob — NO write/exec/network), raised turn
            # budget so the model can grep/read before its envelope.
            argv = [
                binary, "-p",
                "--tools", _DEV_REPO_READ_TOOLS,
                "--output-format", "json",
                "--model", model,
                "--max-turns", str(_DEV_REPO_READ_MAX_TURNS),
            ]
        else:
            # --tools "" is load-bearing: empty allowed-tools = no file/network
            # side effects. --max-turns 1 is belt-and-suspenders. Prompt on stdin
            # (shared runner) — never argv.
            argv = [
                binary, "-p",
                "--tools", "",
                "--output-format", "json",
                "--model", model,
                "--max-turns", "1",
            ]

        start = time.monotonic()
        stdout, stderr, returncode = await run_cli_subprocess(
            argv=argv,
            prompt=prompt,
            timeout_seconds=request.timeout_seconds,
            semaphore=_CLAUDE_SEMAPHORE,
            error_prefix="claude_cli",
            cwd_prefix="errorta-claude-cli-",
            cwd_override=repo_read_root,
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        if returncode != 0:
            # F120: a logged-out CLI can surface the auth error in stdout (the
            # JSON is_error envelope or a 401 line), not only stderr — inspect
            # BOTH so a logged-out Test never degrades to a bare
            # `claude_cli_failed: exit 1:` with no actionable detail.
            low = (stderr + "\n" + stdout).lower()
            if any(t in low for t in (
                "log in", "login", "/login", "not authenticated",
                "authentication", "unauthorized", "401", "403",
            )):
                raise FatalError(
                    "claude_cli_not_authenticated: run 'claude' and log in with your subscription"
                )
            if ("rate" in low and "limit" in low) or "usage limit" in low or "429" in low:
                raise RetryableError("claude_cli_rate_limited")
            raise FatalError(
                f"claude_cli_failed: exit {returncode}: {stderr[:200]}"
            )

        obj = _extract_result_json(stdout)
        if obj is None:
            raise FatalError("claude_cli_unparseable_output")
        if obj.get("is_error"):
            # CLI reported a structured error (e.g. usage limit reached).
            msg = str(obj.get("result") or obj.get("error") or "claude_cli_error")
            low = msg.lower()
            if ("rate" in low and "limit" in low) or "usage limit" in low:
                raise RetryableError(f"claude_cli_rate_limited: {msg[:160]}")
            raise FatalError(f"claude_cli_error: {msg[:200]}")

        content = obj.get("result")
        if not isinstance(content, str) or not content.strip():
            raise FatalError("claude_cli_empty_result")

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
        # Cache tokens (D4: detail only, never headline). The claude CLI
        # prompt-caches the piped prompt, so a cache-heavy turn reports a
        # tiny `input_tokens` with the real bulk in `cache_read_input_tokens`.
        # Mirror async_anthropic.py incl. the cache_creation → cache_write map.
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
        """Cheap detect (NO billable model call): binary provenance + version.

        ``state`` is ``not_installed`` (no binary) or ``installed`` (binary
        resolves; auth is UNKNOWN here — only ``probe_auth`` runs the live,
        billable check). ``login`` is deferred (best-effort/empty).
        """
        details = resolve_cli_binary_detailed(
            [_BINARY],
            override_path=override_path,
            env_var="ERRORTA_CLAUDE_CLI",
            extra_paths=_candidate_claude_paths(),
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
        Returns ``{state, login, detail}`` with state in
        ``connected|logged_out|error``; ``detail`` is redacted.
        """
        result = await self.test_connection(api_key=None)
        return classify_test_result(result)

    def list_routes(self, *, configured: bool) -> list[RouteDescriptor]:
        return list(_DEFAULT_ROUTES)

    def validate_route(self, route_id: str) -> ValidationResult:
        if not route_id.startswith("claude_cli."):
            return ValidationResult(ok=False, reason="route_id must start with 'claude_cli.'")
        if not route_id[len("claude_cli."):]:
            return ValidationResult(ok=False, reason="model name is empty")
        return ValidationResult(ok=True)

    async def test_connection(self, *, api_key: str | None) -> TestConnectionResult:
        """Cheap auth probe: a one-token, tool-free print call."""
        from errorta_council.gateway_local import FatalError, RetryableError

        start = time.monotonic()
        try:
            result = await self.call(
                AsyncProviderRequest(
                    model="haiku",
                    messages=[{"role": "user", "content": "ping"}],
                    max_output_tokens=8,
                    timeout_seconds=30,
                ),
                api_key=None,
            )
        except FatalError as exc:
            latency = int((time.monotonic() - start) * 1000)
            detail = str(exc)
            if "not_installed" in detail:
                return TestConnectionResult(False, "claude CLI not installed", latency)
            if "not_authenticated" in detail:
                return TestConnectionResult(False, "claude CLI not logged in", latency)
            return TestConnectionResult(False, detail[:120], latency)
        except RetryableError as exc:
            latency = int((time.monotonic() - start) * 1000)
            return TestConnectionResult(False, str(exc)[:120], latency)
        latency = int((time.monotonic() - start) * 1000)
        ok = bool(result.content)
        return TestConnectionResult(ok, "subscription CLI ready" if ok else "no response", latency)


async_registry.register("claude_cli", ClaudeCliHandler)


__all__ = ["ClaudeCliHandler"]
