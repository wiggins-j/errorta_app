"""Top-level orchestrator: redact → (summarize) → re-redact → manifest.

Fail-closed gates (invariant 4):
- Unknown destination_scope → blocked('unknown_destination'), no provider call.
- Redaction raises FatalError → blocked('redaction_unavailable'), no provider.
- Summarizer raises FatalError → blocked('summarizer_failed_fatal') unless
  policy.fallback_on_summarizer_fatal allows degrade-to-prompt_only.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import uuid
from typing import Protocol

from .redaction import RedactionPipeline
from .schema import (
    SourceEnvelope,
    SummaryFreshnessAnchors,
    TRANSFORM_FORMAT_VERSION,
    TransformManifest,
    TransformRequest,
    TransformResult,
)
from .store import TransformStore
from .summarization import SummarizerUnavailable, SummaryPipeline

_KNOWN_DESTINATIONS = {"local", "remote", "blocked", "fake"}


class _Redactor(Protocol):
    version: int
    def exclude_disallowed_classes(self, envs, *, destination_scope): ...
    def redact_envelopes(self, envs, *, destination_scope, _enforce_scan: bool = True): ...


class TransformPipeline:
    def __init__(
        self,
        *,
        redaction: _Redactor,
        summary: SummaryPipeline,
        store: TransformStore,
    ) -> None:
        self._redaction = redaction
        self._summary = summary
        self._store = store

    async def transform(self, request: TransformRequest) -> TransformResult:
        # Gate 1: unknown destination — fail closed.
        if request.destination_scope not in _KNOWN_DESTINATIONS:
            manifest_id = "tm-" + uuid.uuid4().hex[:16]
            manifest = self._blocked_manifest(
                request, manifest_id, "unknown_destination", []
            )
            self._store.write_manifest(manifest)
            return TransformResult(
                status="blocked", artifact_id=None, artifact_kind=None,
                content=None, content_sha256=None,
                egress_class="blocked", destination_scope=request.destination_scope,
                token_estimate={"input": 0, "output": 0},
                manifest_id=manifest_id,
                blocked_reason="unknown_destination",
                message_code="unknown_destination",
                warnings=[],
            )

        # Stage A: source-class exclusion.
        kept, dropped = self._redaction.exclude_disallowed_classes(
            list(request.source_envelopes), destination_scope=request.destination_scope
        )

        # Stage B: text redaction. FatalError → blocked.
        from errorta_briefs.connector import FatalError, RetryableError
        try:
            redacted_envs, rule_counts = self._redaction.redact_envelopes(
                kept, destination_scope=request.destination_scope
            )
        except FatalError:
            manifest_id = "tm-" + uuid.uuid4().hex[:16]
            manifest = self._blocked_manifest(
                request, manifest_id, "redaction_unavailable", dropped, rule_counts={}
            )
            self._store.write_manifest(manifest)
            return TransformResult(
                status="blocked", artifact_id=None, artifact_kind=None,
                content=None, content_sha256=None,
                egress_class="blocked", destination_scope=request.destination_scope,
                token_estimate={"input": 0, "output": 0},
                manifest_id=manifest_id,
                blocked_reason="redaction_unavailable",
                message_code="redaction_unavailable",
                warnings=[],
            )

        # Stage C: freshness gate.
        anchors = SummaryFreshnessAnchors(
            transcript_cursor=request.transcript_cursor,
            retrieval_cursor=request.retrieval_cursor,
            source_hashes=sorted(e.content_sha256 for e in redacted_envs),
            corpus_policy_version=request.policy.corpus_policy_version,
            redaction_version=getattr(self._redaction, "version", 1),
            summarizer_version=self._summary.version,
            created_at=_now_iso(),
        )
        key = self._freshness_key(request, anchors)
        cached = self._store.get_fresh_artifact(key=key, anchors=anchors)
        if cached is not None:
            artifact_id, content = cached
            manifest_id = "tm-" + uuid.uuid4().hex[:16]
            manifest = self._allowed_manifest(
                request, manifest_id, artifact_id, anchors, rule_counts, content
            )
            self._store.write_manifest(manifest)
            return self._allowed_result(
                request, manifest_id, artifact_id, content, self._summary.version
            )

        # Stage D: summarize.
        #
        # QA P2 #5 (2026-06-12): the summarizer can raise three distinct
        # error families that all mean "summarization failed" and all
        # must turn into a blocked manifest (invariant 4 — fail closed):
        #
        # 1. ``errorta_briefs.connector.{FatalError,RetryableError}`` —
        #    historically caught here when the summarizer was a brief
        #    connector. Retained for source-class exclusion + redaction
        #    paths that still raise these.
        # 2. ``errorta_council.gateway_local.{FatalError,RetryableError}``
        #    — raised by ``LocalGateway.summarize()`` when the local
        #    Ollama provider misbehaves. Previously escaped this catch
        #    block and crashed the engine because the briefs-connector
        #    exceptions are a SEPARATE class hierarchy.
        # 3. ``SummarizerUnavailable`` — raised by SummaryPipeline when
        #    the gateway is unreachable AND policy forbids the fallback
        #    path. Previously escaped this catch block entirely.
        from errorta_council.gateway_local import (
            FatalError as _CouncilFatal,
            RetryableError as _CouncilRetryable,
        )
        try:
            artifact = await self._summary.summarize(
                redacted_envs, max_output_tokens=request.max_output_tokens
            )
        except (FatalError, _CouncilFatal, SummarizerUnavailable):
            manifest_id = "tm-" + uuid.uuid4().hex[:16]
            manifest = self._blocked_manifest(
                request, manifest_id, "summarizer_failed_fatal",
                dropped, rule_counts=rule_counts,
            )
            self._store.write_manifest(manifest)
            return TransformResult(
                status="blocked", artifact_id=None, artifact_kind=None,
                content=None, content_sha256=None,
                egress_class="blocked", destination_scope=request.destination_scope,
                token_estimate={"input": 0, "output": 0},
                manifest_id=manifest_id,
                blocked_reason="summarizer_failed_fatal",
                message_code="summarizer_failed_fatal",
                warnings=[],
            )
        except (RetryableError, _CouncilRetryable):
            manifest_id = "tm-" + uuid.uuid4().hex[:16]
            manifest = self._blocked_manifest(
                request, manifest_id, "summarizer_failed_retryable",
                dropped, rule_counts=rule_counts,
            )
            self._store.write_manifest(manifest)
            return TransformResult(
                status="blocked", artifact_id=None, artifact_kind=None,
                content=None, content_sha256=None,
                egress_class="blocked", destination_scope=request.destination_scope,
                token_estimate={"input": 0, "output": 0},
                manifest_id=manifest_id,
                blocked_reason="summarizer_failed_retryable",
                message_code="summarizer_failed_retryable",
                warnings=[],
            )

        # Stage E: post-summary re-redact + persist + return allowed.
        post_env = SourceEnvelope(
            class_="summary", corpus_id=None, chunk_id=None, citation_id=None,
            content=artifact.content, content_sha256=artifact.content_sha256,
            tokens=artifact.output_tokens, sensitivity="known_local",
        )
        re_redacted, post_counts = self._redaction.redact_envelopes(
            [post_env], destination_scope=request.destination_scope
        )
        final_content = re_redacted[0].content
        merged_counts = dict(rule_counts)
        for k, v in post_counts.items():
            merged_counts[k] = merged_counts.get(k, 0) + v

        self._store.write_summary(
            artifact_id=artifact.artifact_id, content=final_content, anchors=anchors
        )
        self._store.remember_fresh(
            key=key, anchors=anchors,
            artifact_id=artifact.artifact_id, content=final_content,
        )
        manifest_id = "tm-" + uuid.uuid4().hex[:16]
        manifest = self._allowed_manifest(
            request, manifest_id, artifact.artifact_id,
            anchors, merged_counts, final_content,
        )
        self._store.write_manifest(manifest)
        return self._allowed_result(
            request, manifest_id, artifact.artifact_id, final_content,
            self._summary.version,
        )

    # ---- helpers --------------------------------------------------------

    def _freshness_key(self, req, anchors) -> str:
        s = (
            f"{req.run_id}|{req.member_id}|{req.requested_context_access}"
            f"|{anchors.transcript_cursor}|{anchors.retrieval_cursor}"
            f"|{','.join(anchors.source_hashes)}"
        )
        return hashlib.sha256(s.encode()).hexdigest()

    def _artifact_kind(self, req) -> str:
        return (
            "redacted_summary"
            if req.requested_context_access == "redacted_summary"
            else "summary_only"
        )

    def _allowed_manifest(
        self, req, manifest_id, artifact_id, anchors, rule_counts, content,
    ) -> TransformManifest:
        return TransformManifest(
            format_version=TRANSFORM_FORMAT_VERSION,
            manifest_id=manifest_id,
            run_id=req.run_id, turn_id=req.turn_id, member_id=req.member_id,
            created_at=_now_iso(),
            artifact_kind=self._artifact_kind(req),
            status="allowed",
            source_refs=[
                {"class_": e.class_, "content_sha256": e.content_sha256}
                for e in req.source_envelopes
            ],
            redaction_rule_counts=dict(rule_counts),
            summarizer_route_id=self._summary._route_id,  # noqa: SLF001
            freshness_anchors=anchors,
            payload_sha256=hashlib.sha256(content.encode()).hexdigest(),
            blocked_reason=None,
            warnings=[],
        )

    def _blocked_manifest(
        self, req, manifest_id, reason, dropped, rule_counts=None,
    ) -> TransformManifest:
        return TransformManifest(
            format_version=TRANSFORM_FORMAT_VERSION,
            manifest_id=manifest_id,
            run_id=req.run_id, turn_id=req.turn_id, member_id=req.member_id,
            created_at=_now_iso(),
            artifact_kind=None, status="blocked",
            source_refs=[
                {"class_": e.class_, "content_sha256": e.content_sha256}
                for e in req.source_envelopes
            ],
            redaction_rule_counts=dict(rule_counts or {}),
            summarizer_route_id=None,
            freshness_anchors=None,
            payload_sha256=None,
            blocked_reason=reason,
            warnings=[],
        )

    def _allowed_result(self, req, manifest_id, artifact_id, content, summarizer_version):
        return TransformResult(
            status="allowed",
            artifact_id=artifact_id,
            artifact_kind=self._artifact_kind(req),
            content=content,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            egress_class=req.requested_egress_class,
            destination_scope=req.destination_scope,
            token_estimate={
                "input": sum(e.tokens or 0 for e in req.source_envelopes),
                "output": len(content.split()),
            },
            manifest_id=manifest_id,
            blocked_reason=None,
            message_code=None,
            warnings=[],
        )


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["TransformPipeline"]
