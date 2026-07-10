"""F110 — pull a recommended/selected model into Ollama.

The hardware scan recommends a local model; F110 closes the loop by actually
running ``ollama pull <model>`` (argv-only, no shell) with streaming progress,
plus an installed-models check (``ollama list``) so an already-present model is
skipped.

Security: the model name is the only externally-influenced argument. It is
validated against a strict charset BEFORE it ever reaches argv, so a value like
``--foo`` or ``a; rm -rf`` can never be injected as a flag or shell metacharacter
(we never use a shell anyway). All subprocess calls are argv-list form.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional

from . import settings as settings_module

_LOG = logging.getLogger("errorta_ollama.pull")

# Ollama model references: name[:tag], optionally namespaced/registry-qualified,
# e.g. "llama3.2", "qwen2.5:7b", "library/mistral:latest",
# "registry.example.com/ns/model:tag". Allow letters, digits, and the limited
# set of separators Ollama uses (._-/:). Reject everything else (notably spaces,
# leading '-', shell metacharacters) so the value is safe as a single argv item.
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class InvalidModelName(ValueError):
    """Raised when a model name fails validation (potential flag/shell injection)."""


@dataclass
class PullProgress:
    """A single progress frame emitted during a pull."""

    status: str
    percent: Optional[float] = None
    completed: Optional[int] = None
    total: Optional[int] = None


@dataclass
class PullResult:
    succeeded: bool
    model: str
    message: str
    error: Optional[str] = None


def validate_model_name(model: str) -> str:
    """Return the trimmed model name if valid; raise ``InvalidModelName`` otherwise."""
    candidate = (model or "").strip()
    if not candidate:
        raise InvalidModelName("model name is empty")
    if candidate.startswith("-"):
        # Defensive: the charset already forbids a leading '-', but be explicit.
        raise InvalidModelName("model name may not start with '-'")
    if not _MODEL_RE.match(candidate):
        raise InvalidModelName(f"invalid model name: {model!r}")
    return candidate


def _ollama_bin() -> str:
    """Resolve the Ollama CLI binary. PATH lookup; subprocess does the resolution."""
    return "ollama"


def installed_models() -> List[str]:
    """Return the list of installed model references via ``ollama list``.

    Fail-soft: any error (binary missing, non-zero exit) yields ``[]`` rather
    than raising — callers treat "can't tell" as "not installed".
    """
    try:
        proc = subprocess.run(
            [_ollama_bin(), "list"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _LOG.warning("ollama list failed: %s", exc)
        return []
    if proc.returncode != 0:
        _LOG.warning("ollama list returned %d: %s", proc.returncode, proc.stderr.strip())
        return []
    return _parse_list_output(proc.stdout)


def _parse_list_output(stdout: str) -> List[str]:
    """Parse ``ollama list`` table output → model names (first column).

    Output looks like::

        NAME              ID            SIZE      MODIFIED
        llama3.2:latest   abc123        2.0 GB    2 days ago
        qwen2.5:7b        def456        4.7 GB    1 week ago

    The header row is skipped; the first whitespace-delimited token of each
    remaining row is the model reference.
    """
    names: List[str] = []
    for i, raw in enumerate(stdout.splitlines()):
        line = raw.strip()
        if not line:
            continue
        first = line.split()[0]
        if i == 0 and first.upper() == "NAME":
            # Header row.
            continue
        names.append(first)
    return names


def is_model_installed(model: str) -> bool:
    """True if ``model`` (exact, or with an implicit ``:latest``) is installed."""
    name = validate_model_name(model)
    have = installed_models()
    if name in have:
        return True
    # Ollama treats "foo" and "foo:latest" as the same model.
    if ":" not in name and f"{name}:latest" in have:
        return True
    if name.endswith(":latest") and name[: -len(":latest")] in have:
        return True
    return False


def _parse_pull_line(line: str) -> Optional[PullProgress]:
    """Parse one line of ``ollama pull`` stdout into a PullProgress frame.

    The CLI prints human-readable status lines like::

        pulling manifest
        pulling abc123...  45% ▕████      ▏ 1.2 GB/2.7 GB
        verifying sha256 digest
        success

    We extract a status string and a best-effort percent. The exact spinner
    formatting is version-dependent, so parsing is forgiving — a line we can't
    parse for percent still yields a status-only frame.
    """
    text = line.strip()
    if not text:
        return None
    percent: Optional[float] = None
    m = re.search(r"(\d{1,3})\s*%", text)
    if m:
        try:
            percent = max(0.0, min(100.0, float(m.group(1))))
        except ValueError:
            percent = None
    return PullProgress(status=text, percent=percent)


def pull_model(
    model: str,
    *,
    on_progress: Optional[Callable[[PullProgress], None]] = None,
    timeout: float = 3600.0,
) -> PullResult:
    """Run ``ollama pull <model>`` (argv-only), streaming progress to ``on_progress``.

    - Validates ``model`` first (raises ``InvalidModelName`` on a bad name).
    - Short-circuits if the model is already installed (no pull, success).
    - Streams stdout line-by-line; each parsed line is handed to ``on_progress``.
    - Returns a ``PullResult`` (never raises for a normal pull failure — the
      error is surfaced in the result so the SSE stream can emit a clean frame).
    """
    name = validate_model_name(model)

    if is_model_installed(name):
        if on_progress:
            on_progress(PullProgress(status="already installed", percent=100.0))
        return PullResult(
            succeeded=True,
            model=name,
            message=f"{name} is already installed.",
        )

    # Honor the configured OLLAMA_MODELS storage path for managed installs.
    s = settings_module.load()
    env = None
    if s.storage_path:
        import os

        env = os.environ.copy()
        env["OLLAMA_MODELS"] = s.storage_path

    argv = [_ollama_bin(), "pull", name]
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
    except OSError as exc:
        msg = f"Could not start Ollama: {exc}"
        _LOG.warning("ollama pull spawn failed for %r: %s", name, exc)
        return PullResult(succeeded=False, model=name, message=msg, error=str(exc))

    last_line = ""
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            frame = _parse_pull_line(raw)
            if frame is None:
                continue
            last_line = frame.status
            if on_progress:
                on_progress(frame)
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return PullResult(
            succeeded=False,
            model=name,
            message="Pull timed out.",
            error="timeout",
        )
    except OSError as exc:
        return PullResult(succeeded=False, model=name, message=str(exc), error=str(exc))

    if returncode != 0:
        err = last_line or f"ollama pull exited with code {returncode}"
        return PullResult(succeeded=False, model=name, message=err, error=err)

    return PullResult(succeeded=True, model=name, message=f"Pulled {name}.")
