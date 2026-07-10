"""Persist TransformManifest + SummaryArtifact under ${ERRORTA_HOME}/council/transforms/.

Freshness anchors gate reuse: get_fresh_artifact() returns the artifact
only when ALL anchors match. Stale artifacts are NOT deleted (the store
surfaces them for inspection) but are not returnable as fresh.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from .schema import SummaryFreshnessAnchors, TransformManifest


class TransformStore:
    def __init__(self, *, root: Path) -> None:
        self._root = Path(root)
        (self._root / "manifests").mkdir(parents=True, exist_ok=True)
        (self._root / "summaries").mkdir(parents=True, exist_ok=True)
        # key -> (anchors, artifact_id, content)
        self._fresh: dict[str, tuple[SummaryFreshnessAnchors, str, str]] = {}

    def write_manifest(self, manifest: TransformManifest) -> None:
        path = self._root / "manifests" / f"{manifest.manifest_id}.json"
        path.write_text(json.dumps(asdict(manifest), sort_keys=True, indent=2))

    def write_summary(self, *, artifact_id: str, content: str,
                       anchors: SummaryFreshnessAnchors) -> None:
        path = self._root / "summaries" / f"{artifact_id}.json"
        # Persisted: content_sha256 + anchors + artifact_id + bounded preview.
        # Full content text is not persisted; the router holds it in-memory.
        preview = content[:256]
        payload = {
            "artifact_id": artifact_id,
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "preview": preview,
            "anchors": asdict(anchors),
        }
        path.write_text(json.dumps(payload, sort_keys=True, indent=2))

    def get_fresh_artifact(
        self, *, key: str, anchors: SummaryFreshnessAnchors
    ) -> tuple[str, str] | None:
        cached = self._fresh.get(key)
        if cached is None:
            return None
        cached_anchors, artifact_id, content = cached
        # Anchors equality ignores the created_at timestamp, since freshness
        # is about cursors + hashes + versions, not when the summary fired.
        if _anchors_match(cached_anchors, anchors):
            return artifact_id, content
        return None

    def remember_fresh(
        self,
        *,
        key: str,
        anchors: SummaryFreshnessAnchors,
        artifact_id: str,
        content: str,
    ) -> None:
        self._fresh[key] = (anchors, artifact_id, content)


def _anchors_match(a: SummaryFreshnessAnchors, b: SummaryFreshnessAnchors) -> bool:
    return (
        a.transcript_cursor == b.transcript_cursor
        and a.retrieval_cursor == b.retrieval_cursor
        and list(a.source_hashes) == list(b.source_hashes)
        and a.corpus_policy_version == b.corpus_policy_version
        and a.redaction_version == b.redaction_version
        and a.summarizer_version == b.summarizer_version
    )


__all__ = ["TransformStore"]
