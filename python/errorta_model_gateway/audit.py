"""Append-only model gateway audit log.

Audit records store payload hashes, metadata, and bounded redacted previews.
They never store raw prompts or corpus text.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from . import storage
from .redaction import preview_text as _preview_text

AuditStatus = Literal[
    "planned",
    "ok",
    "blocked_by_policy",
    "blocked_by_budget",
    "provider_error",
]


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def payload_sha256(value: str | None) -> str | None:
    """Hash prompt-ish payloads without retaining plaintext."""
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def preview_text(value: str, *, limit: int = 500) -> str:
    return _preview_text(value, limit=limit)


@dataclass(frozen=True)
class AuditEvent:
    id: str
    created_at: str
    status: AuditStatus
    role: str
    provider: str
    model: str | None
    corpus: str | None
    egress_policy: str
    egress_class: str
    payload_fields: list[str]
    payload_sha256: str | None
    preview_redacted: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    blocked_reason: str | None = None
    session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["request_id"] = self.id
        payload["ts"] = self.created_at
        payload["reason"] = self.blocked_reason
        payload["tokens"] = {
            "input": self.input_tokens,
            "output": self.output_tokens,
        }
        return payload


def build_event(
    *,
    status: AuditStatus,
    role: str,
    provider: str,
    model: str | None,
    corpus: str | None,
    egress_policy: str,
    egress_class: str,
    payload_fields: list[str],
    payload_hash: str | None,
    preview_redacted: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_cost_usd: float = 0.0,
    blocked_reason: str | None = None,
    session_id: str | None = None,
) -> AuditEvent:
    return AuditEvent(
        id=f"mg_{uuid4().hex}",
        created_at=_now_iso_z(),
        status=status,
        role=role,
        provider=provider,
        model=model,
        corpus=corpus,
        egress_policy=egress_policy,
        egress_class=egress_class,
        payload_fields=list(payload_fields),
        payload_sha256=payload_hash,
        preview_redacted=preview_redacted,
        input_tokens=max(0, int(input_tokens)),
        output_tokens=max(0, int(output_tokens)),
        estimated_cost_usd=max(0.0, float(estimated_cost_usd)),
        blocked_reason=blocked_reason,
        session_id=session_id,
    )


def append(event: AuditEvent) -> AuditEvent:
    storage.append_jsonl(storage.audit_path(), event.to_dict())
    return event


def list_events(*, limit: int = 50) -> list[dict[str, Any]]:
    rows = storage.read_jsonl(storage.audit_path())
    try:
        clamped = int(limit)
    except (TypeError, ValueError):
        clamped = 50
    clamped = max(1, min(500, clamped))
    return rows[-clamped:]


def write_json(event: AuditEvent) -> str:
    """Return a stable JSON string for diagnostics/tests."""
    return json.dumps(event.to_dict(), sort_keys=True)
