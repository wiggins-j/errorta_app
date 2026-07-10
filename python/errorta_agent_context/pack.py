"""Pack F035 capsules into prompt-ready text under policy."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from errorta_council.context.tokens import HeuristicEstimator, TokenEstimator

from .refs import ReferenceResolver
from .schema import AgentContextCapsule, CapsuleRef

_REMOTE_BLOCKED = {
    "local_only",
    "secret_possible",
    "contains_secret",
    "raw_corpus",
    "provider_payload",
}


@dataclass(frozen=True)
class PackedCapsule:
    text: str
    included_refs: list[dict[str, Any]] = field(default_factory=list)
    omitted_refs: list[dict[str, Any]] = field(default_factory=list)
    estimated_tokens: int = 0


def pack_capsule(
    capsule: AgentContextCapsule,
    *,
    resolver: ReferenceResolver | None = None,
    resolution: str = "micro",
    destination_scope: str = "local",
    max_tokens: int = 1200,
    estimator: TokenEstimator | None = None,
    include_ref_summaries: bool = True,
) -> PackedCapsule:
    estimator = estimator or HeuristicEstimator()
    lines: list[str] = []
    lines.append(f"format: {capsule.format}")
    lines.append(f"capsule_id: {capsule.capsule_id}")
    lines.append(f"kind: {resolution}")
    if capsule.parent_id:
        lines.append(f"parent_id: {capsule.parent_id}")
    title = capsule.task.get("title") or ""
    status = capsule.task.get("status") or ""
    intent = capsule.task.get("intent") or ""
    lines.extend(["task:", f"  title: {title}", f"  status: {status}", f"  intent: {intent}"])

    for bucket in ("facts", "decisions", "blockers", "open_questions", "next_actions"):
        items = capsule.state.get(bucket) or []
        if not items:
            continue
        lines.append(f"{bucket}:")
        for item in items[:12 if resolution == "micro" else 50]:
            suffix = f" refs={','.join(item.refs)}" if item.refs else ""
            confidence = f" confidence={item.confidence}" if item.confidence else ""
            lines.append(f"  - {item.id}: {item.text}{confidence}{suffix}")

    included: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    ref_entries: list[tuple[str, str, str, dict[str, Any]]] = []
    if capsule.refs:
        for ref in capsule.refs:
            if _blocked(ref, destination_scope):
                omitted.append({"id": ref.id, "uri": ref.uri, "reason": "policy_block"})
                continue
            summary = ref.summary
            sha = ref.sha256
            if include_ref_summaries and resolver is not None:
                resolved = resolver.summarize(ref.uri)
                if resolved.ok:
                    summary = resolved.summary
                    sha = sha or resolved.sha256
                else:
                    omitted.append({"id": ref.id, "uri": ref.uri, "reason": resolved.reason})
                    continue
            included_ref = {"id": ref.id, "uri": ref.uri, "sha256": sha}
            included.append(included_ref)
            ref_entries.append((
                ref.id,
                ref.uri,
                f"  - {ref.id}: {ref.uri} ({ref.class_}; {ref.sensitivity}) {summary}",
                included_ref,
            ))

    candidate = _render(lines, ref_entries)
    while estimator.estimate(candidate) > max_tokens and ref_entries:
        ref_id, ref_uri, _ref_line, included_ref = ref_entries.pop()
        if included_ref in included:
            included.remove(included_ref)
        omitted.append({"id": ref_id, "uri": ref_uri, "reason": "token_budget"})
        candidate = _render(lines, ref_entries)
    return PackedCapsule(
        text=candidate,
        included_refs=included,
        omitted_refs=omitted,
        estimated_tokens=estimator.estimate(candidate),
    )


def _blocked(ref: CapsuleRef, destination_scope: str) -> bool:
    if ref.fetch_policy == "blocked":
        return True
    if destination_scope in {"remote", "remote_provider", "cloud"}:
        return ref.sensitivity in _REMOTE_BLOCKED or ref.fetch_policy in {
            "local_only", "requires_confirmation"
        }
    return False


def _render(lines: list[str], ref_entries: list[tuple[str, str, str, dict[str, Any]]]) -> str:
    if not ref_entries:
        return "\n".join(lines)
    return "\n".join(lines + ["refs:"] + [line for _, _, line, _ in ref_entries])


__all__ = ["PackedCapsule", "pack_capsule"]
