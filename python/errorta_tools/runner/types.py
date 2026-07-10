"""ToolRunner request/result value types.

Runner output is untrusted data. These types keep raw environment values out of
safe projections and cap execution summaries before they can enter Council
context.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Literal

ExecutionLocation = Literal["local", "remote_ssh"]
RunnerStatus = Literal["completed", "failed", "timed_out", "blocked"]

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stable_json_sha256(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode()).hexdigest()


@dataclass(frozen=True)
class EnvGrant:
    """Explicit environment variable grant.

    Values are intentionally excluded from ``safe_projection`` outputs.
    """

    name: str
    value: str

    def __post_init__(self) -> None:
        if not self.name or "=" in self.name or "\x00" in self.name:
            raise ValueError("invalid_env_grant_name")
        if "\x00" in self.value:
            raise ValueError("invalid_env_grant_value")

    def safe_projection(self) -> dict[str, str]:
        return {"name": self.name}


@dataclass(frozen=True)
class RunnerArtifactRef:
    """Reference to an artifact captured by the runner."""

    kind: str
    path: str
    sha256: str
    bytes: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ToolRunnerRequest:
    """Normalized request to execute a bounded runner task."""

    request_id: str
    run_id: str
    tool_call_id: str
    argv: tuple[str, ...]
    workspace_root: str
    relative_cwd: str = "."
    execution_location: ExecutionLocation = "local"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    network_allowed: bool = False
    sandbox: str = "none"
    sandbox_writable_paths: tuple[str, ...] = ()
    sandbox_image: str | None = None
    env_allowlist: tuple[str, ...] = ()
    explicit_env: tuple[EnvGrant, ...] = ()
    artifact_dir: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    requested_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(str(a) for a in self.argv))
        object.__setattr__(
            self,
            "sandbox_writable_paths",
            tuple(str(p) for p in self.sandbox_writable_paths),
        )
        object.__setattr__(self, "env_allowlist", tuple(str(n) for n in self.env_allowlist))
        object.__setattr__(
            self,
            "explicit_env",
            tuple(
                grant if isinstance(grant, EnvGrant) else EnvGrant(**grant)
                for grant in self.explicit_env
            ),
        )
        if not self.request_id:
            raise ValueError("empty_runner_request_id")
        if not self.run_id:
            raise ValueError("empty_runner_run_id")
        if not self.tool_call_id:
            raise ValueError("empty_runner_tool_call_id")
        if not self.argv:
            raise ValueError("empty_runner_argv")
        if self.timeout_seconds <= 0:
            raise ValueError("invalid_runner_timeout")
        if self.max_output_bytes <= 0:
            raise ValueError("invalid_runner_output_cap")

    @property
    def argv_sha256(self) -> str:
        return stable_json_sha256(list(self.argv))

    @property
    def explicit_env_names(self) -> tuple[str, ...]:
        return tuple(grant.name for grant in self.explicit_env)

    def with_metadata(self, **metadata: Any) -> "ToolRunnerRequest":
        merged = dict(self.metadata)
        merged.update(metadata)
        return replace(self, metadata=merged)

    def safe_projection(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "tool_call_id": self.tool_call_id,
            "argv_sha256": self.argv_sha256,
            "argv0": self.argv[0],
            "workspace_root": self.workspace_root,
            "relative_cwd": self.relative_cwd,
            "execution_location": self.execution_location,
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "network_allowed": self.network_allowed,
            "sandbox": self.sandbox,
            "env_allowlist": sorted(set(self.env_allowlist)),
            "explicit_env_names": sorted(set(self.explicit_env_names)),
            "requested_at": self.requested_at,
            "metadata_keys": sorted(str(k) for k in self.metadata.keys()),
        }


@dataclass(frozen=True)
class ToolRunnerResult:
    """Bounded result from a runner execution."""

    request_id: str
    run_id: str
    tool_call_id: str
    status: RunnerStatus
    exit_code: int | None
    duration_ms: int
    stdout_preview: str
    stderr_preview: str
    stdout_sha256: str
    stderr_sha256: str
    stdout_bytes: int
    stderr_bytes: int
    artifact_refs: tuple[RunnerArtifactRef, ...] = ()
    reason_code: str | None = None
    log_tail: str | None = None
    started_at: str = field(default_factory=now_iso)
    finished_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def blocked(
        cls,
        *,
        request: ToolRunnerRequest,
        reason_code: str,
        duration_ms: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> "ToolRunnerResult":
        empty_hash = hashlib.sha256(b"").hexdigest()
        return cls(
            request_id=request.request_id,
            run_id=request.run_id,
            tool_call_id=request.tool_call_id,
            status="blocked",
            exit_code=None,
            duration_ms=duration_ms,
            stdout_preview="",
            stderr_preview="",
            stdout_sha256=empty_hash,
            stderr_sha256=empty_hash,
            stdout_bytes=0,
            stderr_bytes=0,
            reason_code=reason_code,
            log_tail=None,
            metadata=dict(metadata or {}),
        )

    def audit_projection(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "tool_call_id": self.tool_call_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "stdout_preview": self.stdout_preview,
            "stderr_preview": self.stderr_preview,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "artifact_refs": [ref.to_dict() for ref in self.artifact_refs],
            "reason_code": self.reason_code,
            "log_tail": self.log_tail,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "metadata": {
                "keys": sorted(str(k) for k in self.metadata.keys()),
                "sha256": stable_json_sha256(self.metadata) if self.metadata else None,
            },
        }


__all__ = [
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "EnvGrant",
    "ExecutionLocation",
    "RunnerArtifactRef",
    "RunnerStatus",
    "ToolRunnerRequest",
    "ToolRunnerResult",
    "now_iso",
    "stable_json_sha256",
]
