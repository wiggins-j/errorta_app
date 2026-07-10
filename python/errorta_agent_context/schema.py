"""F035 agent context capsule schema.

The schema stays intentionally small and typed. Large evidence is always a ref.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# capsule_id / delta capsule_id must match this to prevent path traversal.
_CAPSULE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _validate_capsule_id(capsule_id: str) -> None:
    if not _CAPSULE_ID_RE.fullmatch(str(capsule_id)):
        raise ValueError(
            f"invalid capsule_id {capsule_id!r}: must match [A-Za-z0-9_-]{{1,128}}"
        )

CAPSULE_FORMAT = "errorta.agent_context_capsule.v1"
DELTA_FORMAT = "errorta.agent_context_delta.v1"
CONFIDENCE_VALUES = {"high", "medium", "low"}
SENSITIVITY_VALUES = {
    "public",
    "safe_metadata",
    "user_visible",
    "local_only",
    "secret_possible",
    "contains_secret",
    "raw_corpus",
    "provider_payload",
}
FETCH_POLICY_VALUES = {
    "inline_ok",
    "summary_only",
    "redacted_preview_only",
    "local_only",
    "requires_confirmation",
    "blocked",
}


@dataclass(frozen=True)
class StateItem:
    id: str
    text: str
    status: str | None = None
    priority: str | None = None
    owner: str | None = None
    refs: list[str] = field(default_factory=list)
    confidence: Literal["high", "medium", "low"] | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StateItem":
        confidence = raw.get("confidence")
        if confidence is not None and confidence not in CONFIDENCE_VALUES:
            raise ValueError(f"unknown confidence {confidence!r}")
        return cls(
            id=str(raw["id"]),
            text=str(raw["text"]),
            status=raw.get("status"),
            priority=raw.get("priority"),
            owner=raw.get("owner"),
            refs=[str(r) for r in raw.get("refs") or []],
            confidence=confidence,
        )


@dataclass(frozen=True)
class CapsuleRef:
    id: str
    uri: str
    class_: str
    sensitivity: str = "safe_metadata"
    sha256: str | None = None
    summary: str = ""
    fetch_policy: str = "summary_only"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CapsuleRef":
        sensitivity = str(raw.get("sensitivity") or "safe_metadata")
        fetch_policy = str(raw.get("fetch_policy") or "summary_only")
        if sensitivity not in SENSITIVITY_VALUES:
            raise ValueError(f"unknown sensitivity {sensitivity!r}")
        if fetch_policy not in FETCH_POLICY_VALUES:
            raise ValueError(f"unknown fetch_policy {fetch_policy!r}")
        return cls(
            id=str(raw["id"]),
            uri=str(raw["uri"]),
            class_=str(raw.get("class_") or raw.get("class") or "artifact"),
            sensitivity=sensitivity,
            sha256=raw.get("sha256"),
            summary=str(raw.get("summary") or ""),
            fetch_policy=fetch_policy,
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["class"] = d.pop("class_")
        return d


@dataclass(frozen=True)
class AgentContextCapsule:
    capsule_id: str
    kind: Literal["micro", "brief", "full"]
    created_at: str
    task: dict[str, Any]
    scope: dict[str, Any] = field(default_factory=dict)
    state: dict[str, list[StateItem]] = field(default_factory=dict)
    refs: list[CapsuleRef] = field(default_factory=list)
    policy: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    created_by: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None
    digest: dict[str, Any] = field(default_factory=dict)
    format: str = CAPSULE_FORMAT

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AgentContextCapsule":
        if str(raw.get("format") or CAPSULE_FORMAT) != CAPSULE_FORMAT:
            raise ValueError("unsupported capsule format")
        _validate_capsule_id(str(raw.get("capsule_id") or ""))
        state: dict[str, list[StateItem]] = {}
        for key, items in (raw.get("state") or {}).items():
            state[str(key)] = [StateItem.from_dict(i) for i in (items or [])]
        refs = [CapsuleRef.from_dict(r) for r in raw.get("refs") or []]
        kind = str(raw.get("kind") or "micro")
        if kind not in {"micro", "brief", "full"}:
            raise ValueError(f"unknown capsule kind {kind!r}")
        capsule = cls(
            capsule_id=str(raw["capsule_id"]),
            kind=kind,  # type: ignore[arg-type]
            parent_id=raw.get("parent_id"),
            created_at=str(raw["created_at"]),
            created_by=dict(raw.get("created_by") or {}),
            scope=dict(raw.get("scope") or {}),
            task=dict(raw.get("task") or {}),
            policy=dict(raw.get("policy") or {}),
            state=state,
            refs=refs,
            limits=dict(raw.get("limits") or {}),
            digest=dict(raw.get("digest") or {}),
        )
        expected = capsule.canonical_sha256()
        recorded = str(capsule.digest.get("canonical_sha256") or "")
        if recorded and recorded != expected:
            raise ValueError("capsule digest mismatch")
        return capsule

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "format": self.format,
            "capsule_id": self.capsule_id,
            "kind": self.kind,
            "parent_id": self.parent_id,
            "created_at": self.created_at,
            "created_by": dict(self.created_by),
            "scope": dict(self.scope),
            "task": dict(self.task),
            "policy": dict(self.policy),
            "state": {
                key: [asdict(item) for item in items]
                for key, items in sorted(self.state.items())
            },
            "refs": [ref.to_dict() for ref in self.refs],
            "limits": dict(self.limits),
        }
        if include_digest:
            digest = dict(self.digest)
            digest["canonical_sha256"] = self.canonical_sha256()
            digest["schema_version"] = CAPSULE_FORMAT
            data["digest"] = digest
        return data

    def canonical_sha256(self) -> str:
        return _hash(self.to_dict(include_digest=False))


@dataclass(frozen=True)
class AgentContextDelta:
    capsule_id: str
    parent_id: str
    created_at: str
    changes: dict[str, Any]
    format: str = DELTA_FORMAT

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AgentContextDelta":
        if str(raw.get("format") or DELTA_FORMAT) != DELTA_FORMAT:
            raise ValueError("unsupported delta format")
        _validate_capsule_id(str(raw.get("capsule_id") or ""))
        if raw.get("parent_id") is not None:
            _validate_capsule_id(str(raw["parent_id"]))
        return cls(
            capsule_id=str(raw["capsule_id"]),
            parent_id=str(raw["parent_id"]),
            created_at=str(raw["created_at"]),
            changes=dict(raw.get("changes") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _hash(data: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


__all__ = [
    "AgentContextCapsule",
    "AgentContextDelta",
    "CAPSULE_FORMAT",
    "CONFIDENCE_VALUES",
    "CapsuleRef",
    "StateItem",
    "_validate_capsule_id",
]
