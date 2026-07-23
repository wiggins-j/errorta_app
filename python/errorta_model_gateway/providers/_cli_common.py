"""F040 — shared subprocess plumbing for the CLI-backed providers.

Both ``async_claude_cli`` and ``async_codex_cli`` shell out to an
already-logged-in vendor CLI. The spawn + concurrency cap + timeout/kill
cascade + output cap are identical and security-sensitive, so they live here so
a fix lands once (code review 2026-06-13, findings #1/#2/#22).

Guarantees:
- Spawn happens **inside** the semaphore, so the cap bounds concurrent
  *processes*, not just their I/O (finding #2).
- On timeout: ``terminate`` → grace ``wait`` → ``kill`` → grace ``wait`` again,
  so a killed child is **reaped** (no zombie; finding #1).
- The prompt is fed on **stdin** (never argv → no ARG_MAX / ``ps`` leak).
- The subprocess runs in an **isolated empty temp cwd** so the sidecar's
  project config can't contaminate the prompt.
- stdout/stderr are byte-capped before decode.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
# Cheap `<cli> --version` detect must never hang the settings panel.
_VERSION_PROBE_TIMEOUT_SECONDS = 5.0
_TERMINATE_GRACE_SECONDS = 3.0

# F087 Slice 0: how many CLI invocations of one provider may run at once. The
# default (10) lets a multi-member coding team fan out widely; a concurrent
# coding run may still raise it further toward max_parallel_workers (never above
# the provider/session cap) via the per-provider setters. Overridable at import
# with ERRORTA_CLI_MAX_CONCURRENCY. NOTE: these are Errorta's own subprocess
# guards, not a vendor-imposed limit — running this wide can trip the vendor's
# subscription usage/rate limits (handled as retryable) and uses more memory.
_DEFAULT_CLI_CONCURRENCY = 10


def initial_cli_concurrency() -> int:
    raw = os.environ.get("ERRORTA_CLI_MAX_CONCURRENCY", "")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_CLI_CONCURRENCY
    return n if n >= 1 else _DEFAULT_CLI_CONCURRENCY

# Loader-path vars PyInstaller injects into the frozen process. If the spawned
# vendor CLI (and the Node/other runtime it shells to) inherits them, it loads
# the app bundle's dylibs instead of the system ones and crashes — the classic
# "works from a shell, fails only inside the .app" failure. PyInstaller saves
# each var's pre-launch value in ``<VAR>_ORIG``; restore that, else drop it.
_LOADER_ENV_VARS = (
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "DYLD_INSERT_LIBRARIES",
    "LD_LIBRARY_PATH",
)

# R3 — the sidecar's own mutation-auth secrets. They exist so the sidecar can
# validate its own inbound requests; a spawned vendor CLI has no business seeing
# them, and leaking the bearer to an external process would defeat the token
# gate. Scrubbed from every subprocess env built here. (String literals, not an
# import, so this stdlib-only module keeps its no-errorta_app dependency.)
_SIDECAR_AUTH_ENV_VARS = (
    "ERRORTA_SIDECAR_TOKEN",
    "ERRORTA_SIDECAR_TOKEN_ENFORCE",
)


def _clean_subprocess_env() -> dict[str, str]:
    """Environment for the spawned CLI: PyInstaller loader pollution removed,
    PATH augmented with common toolchain dirs (a GUI .app inherits a minimal
    PATH that excludes ~/.local/bin, /opt/homebrew/bin, etc.)."""
    env = dict(os.environ)
    for var in _LOADER_ENV_VARS:
        original = env.pop(f"{var}_ORIG", None)
        if original:
            env[var] = original
        else:
            env.pop(var, None)
    # R3: never hand the sidecar's mutation-auth secret to a spawned vendor CLI.
    for var in _SIDECAR_AUTH_ENV_VARS:
        env.pop(var, None)
    extra = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(Path.home() / ".local/bin"),
        "/usr/bin",
        "/bin",
    ]
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    for entry in extra:
        if entry not in parts:
            parts.append(entry)
    env["PATH"] = os.pathsep.join(parts)
    return env


def _is_executable_file(p: str) -> bool:
    return bool(p) and os.path.isfile(p) and os.access(p, os.X_OK)


def resolve_cli_binary_detailed(
    names: list[str],
    *,
    override_path: str | None = None,
    env_var: str | None = None,
    extra_paths: list[str] | None = None,
) -> dict[str, str] | None:
    """Resolve a vendor CLI, reporting *how* it was found.

    Returns ``{"path", "source", "name_used"}`` or ``None``. ``source`` is one
    of ``override_settings`` / ``override_env`` / ``path`` / ``common_dir`` /
    ``app_bundle``.

    Precedence (F040-01): a caller-supplied ``override_path`` (read from the
    app's ``settings.json`` and passed in — this module stays stdlib-only and
    never reads settings) wins, then the ``env_var`` override, then the
    GUI-augmented PATH, then common toolchain dirs, then ``extra_paths``
    (vendor app bundles).

    A frozen macOS app launched from Finder/Dock often has a minimal PATH, so
    ``shutil.which("tool")`` alone misses CLIs installed into ``~/.local/bin``,
    Homebrew, or vendor app bundles.
    """
    if override_path and _is_executable_file(override_path):
        return {
            "path": override_path,
            "source": "override_settings",
            "name_used": os.path.basename(override_path),
        }

    if env_var:
        override = os.environ.get(env_var)
        if override and _is_executable_file(override):
            return {
                "path": override,
                "source": "override_env",
                "name_used": os.path.basename(override),
            }

    env_path = _clean_subprocess_env().get("PATH", "")
    for name in names:
        found = shutil.which(name, path=env_path)
        if found:
            return {"path": found, "source": "path", "name_used": name}

    # Common toolchain dirs (a GUI .app inherits a minimal PATH excluding these).
    home = Path.home()
    common_seen: set[str] = set()
    for name in names:
        for cand in (
            str(home / ".local" / "bin" / name),
            str(home / "bin" / name),
            f"/opt/homebrew/bin/{name}",
            f"/usr/local/bin/{name}",
            f"/usr/bin/{name}",
        ):
            if cand in common_seen:
                continue
            common_seen.add(cand)
            if _is_executable_file(cand):
                return {"path": cand, "source": "common_dir", "name_used": name}

    # Vendor app bundles (and any other caller-supplied fallbacks).
    if extra_paths:
        seen: set[str] = set()
        for cand in extra_paths:
            if cand in seen:
                continue
            seen.add(cand)
            if _is_executable_file(cand):
                return {
                    "path": cand,
                    "source": "app_bundle",
                    "name_used": os.path.basename(cand),
                }
    return None


def resolve_cli_binary(
    names: list[str],
    *,
    override_path: str | None = None,
    env_var: str | None = None,
    extra_paths: list[str] | None = None,
) -> str | None:
    """Back-compat thin wrapper over :func:`resolve_cli_binary_detailed`.

    Returns just the resolved path (or ``None``). Existing callers keep working;
    new callers wanting provenance use ``resolve_cli_binary_detailed``.
    """
    details = resolve_cli_binary_detailed(
        names,
        override_path=override_path,
        env_var=env_var,
        extra_paths=extra_paths,
    )
    return details["path"] if details else None


def cli_version(path: str) -> str | None:
    """Best-effort ``<bin> --version``. Time-boxed; redacted; never raises.

    Used on the cheap auto-detect path (settings open / focus). Returns the
    first non-empty stripped line of stdout (or stderr fallback), or ``None``.
    The version string is run through the diagnostics redactor so a CLI that
    prints a path/username can't leak it.
    """
    if not _is_executable_file(path):
        return None
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, time-boxed
            [path, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=tempfile.gettempdir(),
            env=_clean_subprocess_env(),
            timeout=_VERSION_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = (proc.stdout or b"")[:4096].decode("utf-8", "replace").strip()
    if not raw:
        raw = (proc.stderr or b"")[:4096].decode("utf-8", "replace").strip()
    if not raw:
        return None
    first = raw.splitlines()[0].strip()
    if not first:
        return None
    from errorta_diagnostics import redact

    redacted, _ = redact.redact_home_path(first)
    redacted, _ = redact.redact_username(redacted)
    redacted, _ = redact.redact_tokens(redacted)
    return redacted[:120]


def run_cli_capture(argv: list[str], *, timeout: float = 8.0) -> str | None:
    """Best-effort sync capture of a cheap local CLI command's stdout.

    Fixed argv (no shell), clean env, time-boxed, ``cwd`` in tmp. Returns the
    decoded stdout (capped) on a zero exit, else ``None`` — never raises. Intended
    for cheap *non-billable* list/probe subcommands (e.g. ``<cli> models``) used
    to populate route dropdowns; it is NOT a model call. The caller is
    responsible for parsing and for falling back to a static list when this
    returns ``None`` (CLI absent, error exit, timeout)."""
    if not argv or not _is_executable_file(argv[0]):
        return None
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, time-boxed
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=tempfile.gettempdir(),
            env=_clean_subprocess_env(),
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or b"")[:65536].decode("utf-8", "replace")


def classify_test_result(result: Any) -> dict[str, Any]:
    """Map a ``TestConnectionResult`` into the F040-01 auth-state shape.

    Returns ``{state, login, detail, remediation}`` where ``state`` is one of
    ``connected`` / ``logged_out`` / ``error``. F120: the state is now derived
    from the SAME classifier the run-loop uses (``member_health``), so a
    logged-out CLI reads ``logged_out`` whether the underlying failure was a
    "not authenticated" stderr, a 401 ``is_error`` stdout, OR a bare nonzero
    exit whose message mentions auth — never a bare ``claude_cli_failed: exit
    1:``. ``detail`` is redacted (defense-in-depth) before it leaves this layer;
    ``login`` is best-effort and deferred (empty today)."""
    detail = str(getattr(result, "detail", "") or "")
    ok = bool(getattr(result, "ok", False))

    if ok:
        state = "connected"
        remediation = ""
    else:
        # Reuse the F120 member-health classifier so setup-time wording matches
        # the run-loop's classification exactly.
        from errorta_council.coding.member_health import (
            AUTH_FAILED,
            RATE_LIMITED,
            classify_member_failure,
        )
        failure = classify_member_failure(detail)
        if failure.status == AUTH_FAILED:
            state = "logged_out"
        elif failure.status == RATE_LIMITED:
            # F132: a throttled CLI is connected-but-busy, not a broken
            # integration — surface it as its own state so the UI can render an
            # amber "you're connected; try again later" instead of a red failure.
            state = "rate_limited"
        else:
            # binary_missing + everything else: a clear detail + remediation.
            state = "error"
        remediation = failure.remediation

    from errorta_diagnostics import redact

    safe, _ = redact.redact_home_path(detail)
    safe, _ = redact.redact_username(safe)
    safe, _ = redact.redact_tokens(safe)
    return {"state": state, "login": "", "detail": safe[:200], "remediation": remediation}


def flatten_messages(messages: list[dict[str, str]]) -> str:
    """Flatten role/content turns into a single stdin prompt.

    System messages first, then the conversation as ``ROLE: content``.
    """
    system_parts: list[str] = []
    body_parts: list[str] = []
    for msg in messages:
        role = (msg.get("role") or "user").lower()
        content = msg.get("content") or ""
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
        else:
            body_parts.append(f"{role.upper()}: {content}")
    return "\n\n".join(system_parts + body_parts)


async def _terminate_then_kill(proc, grace: float) -> None:
    """SIGTERM → grace → SIGKILL → grace, reaping the child either way."""
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
        return
    except asyncio.TimeoutError:
        pass
    proc.kill()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        # Best-effort: the OS will reap once the process actually dies; we
        # don't block the council turn any longer.
        pass


async def run_cli_subprocess(
    *,
    argv: list[str],
    prompt: str,
    timeout_seconds: int,
    semaphore: asyncio.Semaphore,
    error_prefix: str,
    cwd_prefix: str,
    max_bytes: int = _MAX_OUTPUT_BYTES,
    grace: float = _TERMINATE_GRACE_SECONDS,
) -> tuple[str, str, int]:
    """Run one CLI invocation; return (stdout, stderr, returncode).

    Raises ``FatalError`` (binary missing) / ``RetryableError`` (timeout) from
    ``errorta_council.gateway_local`` (imported lazily to avoid a cycle).
    """
    from errorta_council.gateway_local import FatalError, RetryableError

    with tempfile.TemporaryDirectory(prefix=cwd_prefix) as cwd:
        async with semaphore:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=_clean_subprocess_env(),
                    start_new_session=True,  # so kill reaches child processes
                )
            except FileNotFoundError:
                raise FatalError(
                    f"{error_prefix}_not_installed: CLI binary not found"
                ) from None
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(input=prompt.encode("utf-8")),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                await _terminate_then_kill(proc, grace)
                raise RetryableError(f"{error_prefix}_timeout") from None

    stdout = (stdout_b or b"")[:max_bytes].decode("utf-8", "replace")
    stderr = (stderr_b or b"")[:max_bytes].decode("utf-8", "replace")
    return stdout, stderr, proc.returncode
