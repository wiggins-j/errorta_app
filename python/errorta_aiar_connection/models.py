"""Shared data models for the AIAR connection authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

AiarRuntimeKind = Literal[
    "local-aiar",
    "aiar-service",
    "errorta-sidecar-remote",
    "disconnected",
]


@dataclass(frozen=True)
class AiarCapabilities:
    answer: bool = False
    judge: bool = False
    model_catalog: bool = False
    model_active_status: bool = False
    model_set_active: bool = False
    ollama_pull: bool = False
    corpus_list: bool = False
    corpus_upload: bool = False
    folder_watch: bool = False
    pure_retrieve: bool = False
    grounding_record: bool = False
    grounding_lookup: bool = False
    remote_ingest: bool = False
    diagnostics: str = "metadata-only"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AiarRuntime:
    kind: AiarRuntimeKind
    display_name: str
    connected: bool
    capabilities: AiarCapabilities = field(default_factory=AiarCapabilities)
    base_url: str | None = None
    token: str | None = None
    verify_tls: bool = True
    timeout_s: float = 60.0
    backend_id: str | None = None
    active_model: str | None = None
    active_model_ready: bool | None = None
    available_models: list[str] = field(default_factory=list)
    corpus_count: int | None = None
    config_source: str = "none"
    status_source: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "runtime_kind": self.kind,
            "display_name": self.display_name,
            "connected": self.connected,
            "base_url": self.base_url,
            "token_configured": bool(self.token),
            "verify_tls": self.verify_tls,
            "timeout_s": self.timeout_s,
            "backend_id": self.backend_id,
            "capabilities": self.capabilities.to_dict(),
            "active_model": self.active_model,
            "active_model_ready": self.active_model_ready,
            "available_models": list(self.available_models),
            "corpus_count": self.corpus_count,
            "config_source": self.config_source,
            "status_source": self.status_source,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


def disconnected(
    *,
    display_name: str = "AIAR disconnected",
    config_source: str = "none",
    error_code: str | None = None,
    error_message: str | None = None,
) -> AiarRuntime:
    return AiarRuntime(
        kind="disconnected",
        display_name=display_name,
        connected=False,
        config_source=config_source,
        error_code=error_code,
        error_message=error_message,
    )
