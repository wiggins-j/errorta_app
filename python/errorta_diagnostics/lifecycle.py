"""F048 — sidecar + ToolRunner lifecycle diagnostics.

Adapts Omnigent's local-server lifecycle patterns (pid / config signature /
short log tail) to Errorta's Tauri-launched sidecar and the future F043
ToolRunner — without adding a long-lived host daemon.

Everything here is **local and redacted**: config signatures are sha256 over
non-secret settings, log tails run through the existing redaction pipeline, and
nothing is ever uploaded. Volatile fields (pid, timestamps, ports) are excluded
from signatures so the signature is stable across restarts with identical
config and *changes* only when a restart-relevant setting changes.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from errorta_app import __version__ as SIDECAR_VERSION
from . import redact

# Run-lifecycle status vocabulary shared by the sidecar and ToolRunner records.
RUNNER_LIFECYCLE_FORMAT_VERSION = 1
RUNNER_STATUSES = frozenset(
    {"starting", "running", "completed", "failed", "cancelled"}
)


# --- signature inputs (fail-soft; never raise) -----------------------------

def _residency_mode() -> str:
    try:
        from errorta_residency import config as residency_config

        return str(getattr(residency_config.load(), "mode", "local") or "local")
    except Exception:
        return "local"


def _remote_host_id() -> str | None:
    try:
        from errorta_residency import config as residency_config

        state = residency_config.load()
        # A stable identifier for the remote target — never credentials. The
        # residency state stores ``ssh_host`` (SSH-remote mode) / ``cloud_url``
        # (Cloud mode); a change here must move the config signature.
        host = getattr(state, "ssh_host", None) or getattr(state, "cloud_url", None)
        return str(host) if host else None
    except Exception:
        return None


def _ollama_host() -> str | None:
    try:
        from errorta_ollama import settings as ollama_settings

        return str(getattr(ollama_settings.load(), "host", "") or "") or None
    except Exception:
        return os.environ.get("ERRORTA_OLLAMA_HOST") or None


def _log_level() -> str:
    try:
        from errorta_app import settings as app_settings

        return str(app_settings.load().get("log_level", "info") or "info")
    except Exception:
        return "info"


def _configured_providers() -> list[str]:
    """Which providers are configured (names only — never key values)."""
    try:
        from errorta_app import provider_keys

        keys = provider_keys.load_all()
        out: list[str] = []
        for cls, entry in keys.items():
            if cls == "custom":
                if entry:
                    out.append("custom")
            elif isinstance(entry, dict) and entry.get("api_key"):
                out.append(cls)
        return sorted(out)
    except Exception:
        return []


def collect_signature_inputs() -> dict[str, Any]:
    """The non-volatile, non-secret settings a sidecar restart depends on."""
    return {
        "sidecar_version": SIDECAR_VERSION,
        "residency_mode": _residency_mode(),
        "remote_host_id": _remote_host_id(),
        "ollama_host": _ollama_host(),
        "configured_providers": _configured_providers(),
        "log_level": _log_level(),
    }


def config_signature(inputs: dict[str, Any] | None = None) -> str:
    """Deterministic signature over restart-relevant settings.

    Stable across restarts with identical config; changes when a relevant
    setting changes. Excludes pid/timestamps/ports.
    """
    data = inputs if inputs is not None else collect_signature_inputs()
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return "cfg-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


# Signature inputs that can reveal private infrastructure (a tailnet Ollama
# host, an SSH target, a private cloud URL). The config SIGNATURE is computed
# from the RAW inputs — so it still changes when one of these changes — but the
# echoed ``signature_inputs`` masks them to a presence flag. This keeps the
# diagnostic bundle (which includes lifecycle.json) free of private hostnames;
# the generic redaction pipeline only catches IPs/$HOME/tokens, not hostnames.
_HOST_BEARING_INPUTS = ("ollama_host", "remote_host_id")


def sidecar_lifecycle() -> dict[str, Any]:
    """Liveness + config-signature metadata for the running sidecar."""
    inputs = collect_signature_inputs()
    safe_inputs = dict(inputs)
    for key in _HOST_BEARING_INPUTS:
        safe_inputs[key] = bool(inputs.get(key))
    return {
        "component": "sidecar",
        "pid": os.getpid(),
        "sidecar_version": SIDECAR_VERSION,
        "residency_mode": inputs["residency_mode"],
        "config_signature": config_signature(inputs),
        # Echo the non-secret inputs (host-bearing fields masked to a presence
        # flag) so the frontend/bundle can show WHAT changed, not just that the
        # signature moved.
        "signature_inputs": safe_inputs,
    }


# --- redacted log tails ----------------------------------------------------

def redacted_log_tail(
    log_buffer: Any, *, lines: int = 50, max_chars: int = 8000
) -> dict[str, Any]:
    """Capped + redacted tail of the in-memory log buffer.

    ``log_buffer`` is the app's ``LogBuffer`` (duck-typed: needs ``.tail(n)``).
    Returns a dict with redacted lines + per-rule redaction counts so the
    diagnostics bundle and any failure report carry no raw secrets/paths.
    """
    tail_fn = getattr(log_buffer, "tail", None)
    raw_lines = list(tail_fn(lines)) if callable(tail_fn) else []
    full = "\n".join(str(line) for line in raw_lines)
    truncated = len(full) > max_chars
    clipped = full[-max_chars:] if truncated else full
    redacted, counts = redact.apply_pipeline(
        clipped,
        home=os.environ.get("HOME"),
        username=os.environ.get("USER"),
    )
    return {
        "lines": redacted.splitlines(),
        "redaction_counts": counts,
        "truncated": truncated,
    }


# --- ToolRunner lifecycle records (F043 writes these; schema lives here) ----

def runner_lifecycle_record(
    *,
    run_id: str,
    runner_id: str,
    status: str,
    pid: int | None = None,
    log_path: str | None = None,
    config_signature_value: str | None = None,
    exit_code: int | None = None,
    failure_tail: dict[str, Any] | None = None,
    created_at: str,
) -> dict[str, Any]:
    """A per-run ToolRunner lifecycle record (F043 consumes this).

    ``failure_tail`` must already be a ``redacted_log_tail`` result — raw log
    text never belongs in a lifecycle record.
    """
    if status not in RUNNER_STATUSES:
        raise ValueError(f"unknown_runner_status: {status}")
    record: dict[str, Any] = {
        "format_version": RUNNER_LIFECYCLE_FORMAT_VERSION,
        "component": "tool_runner",
        "run_id": run_id,
        "runner_id": runner_id,
        "status": status,
        "created_at": created_at,
    }
    if pid is not None:
        record["pid"] = pid
    if log_path is not None:
        record["log_path"] = log_path
    if config_signature_value is not None:
        record["config_signature"] = config_signature_value
    if exit_code is not None:
        record["exit_code"] = exit_code
    if failure_tail is not None:
        record["failure_tail"] = failure_tail
    return record


def write_runner_lifecycle(runs_dir: Path, record: dict[str, Any]) -> Path:
    """Atomic write of a runner lifecycle record under the run directory."""
    import tempfile

    run_id = str(record["run_id"])
    runner_id = str(record["runner_id"])
    for value, name in ((run_id, "run_id"), (runner_id, "runner_id")):
        if not value or "/" in value or ".." in value:
            raise ValueError(f"unsafe_{name}")
    target_dir = Path(runs_dir) / "runner-lifecycle" / run_id
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{runner_id}.json"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=target_dir)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, sort_keys=True, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    return path
