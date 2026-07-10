"""F031-07 SummaryPipeline.

Calls LocalGateway.summarize() (invariant 3 — never opens its own HTTP).
Extractive fallback when no local model is available AND policy permits.
Returns a SummaryArtifact (not the same as a TransformManifest — pipeline.py
wraps it).

This module MUST NOT import httpx, anthropic, openai, or any provider SDK.
The import-lint test enforces that.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Protocol

from .schema import SourceEnvelope

SUMMARIZER_VERSION = 1


class SummarizerUnavailable(Exception):
    """Local gateway unreachable AND fallback not permitted by policy."""


@dataclass(frozen=True)
class SummaryArtifact:
    artifact_id: str
    content: str
    content_sha256: str
    summary_mode: str
    summarizer_route_id: str
    summarizer_version: int
    input_tokens: int | None
    output_tokens: int | None
    source_hashes: list[str]


class _SummarizeAware(Protocol):
    async def summarize(self, request): ...
    async def is_reachable(self) -> bool: ...


# F031-07 hardening: minimum contiguous-character match between a produced
# summary and any source envelope content that we treat as a leak. The
# RedactionPipeline only scans for known sentinel patterns (home paths,
# env vars, provider tokens, IPs, hostnames) — arbitrary corpus content
# (aerospace specs, legal jargon, regulatory text) passes through untouched.
# When the extractive fallback echoes a chunk whose first sentence is the
# whole chunk, or when an abstractive summarizer regurgitates input, the
# redacted_summary path would surface raw corpus bytes despite policy.
# This threshold catches that. 40 chars is roughly a long phrase — short
# enough that random co-occurrence is vanishingly unlikely, long enough
# that a genuine paraphrase rarely contains a 40-char verbatim window.
_SUMMARY_SUBSTRING_LEAK_THRESHOLD_DEFAULT = 40


class SummaryPipeline:
    def __init__(
        self,
        *,
        gateway: _SummarizeAware,
        route_id: str,
        allow_extractive_fallback: bool = True,
        version: int = SUMMARIZER_VERSION,
        summary_substring_leak_threshold: int = (
            _SUMMARY_SUBSTRING_LEAK_THRESHOLD_DEFAULT
        ),
    ) -> None:
        self._gateway = gateway
        self._route_id = route_id
        self._allow_extractive_fallback = allow_extractive_fallback
        self.version = version
        # 0 disables the gate; tests can flip this, production keeps the default.
        self._leak_threshold = int(summary_substring_leak_threshold)

    async def summarize(
        self,
        envelopes: list[SourceEnvelope],
        *,
        max_output_tokens: int,
        timeout_seconds: int = 30,
    ) -> SummaryArtifact:
        reachable = False
        try:
            reachable = await self._gateway.is_reachable()
        except Exception:
            reachable = False

        source_hashes = [e.content_sha256 for e in envelopes]

        if not reachable:
            if not self._allow_extractive_fallback:
                raise SummarizerUnavailable("local gateway unreachable, fallback forbidden")
            # QA P1 #1 (2026-06-12): the extractive fallback echoes first
            # sentences from source envelopes, so by construction it can
            # leak verbatim source bytes shorter than the substring-leak
            # gate's threshold. For the redacted_summary access level
            # (the only caller of SummaryPipeline today) policy says no
            # verbatim source bytes. Skip the extractive path entirely
            # and go straight to structural metadata. The label is
            # "structural" so downstream tells the UI/operator "fallback
            # fired" rather than implying real summarization happened.
            content = self._structural_fallback(envelopes)
            return SummaryArtifact(
                artifact_id="sa-" + uuid.uuid4().hex[:16],
                content=content,
                content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                summary_mode="structural",
                summarizer_route_id=self._route_id,
                summarizer_version=self.version,
                input_tokens=None, output_tokens=None,
                source_hashes=source_hashes,
            )

        from errorta_council.gateway_local import SummaryRequest
        system_msg = {"role": "system", "content":
            "Summarize the provided sources for downstream Council context. "
            "Preserve factual content; drop chrome."}
        user_msg = {"role": "user", "content":
            "\n\n---\n\n".join(e.content for e in envelopes)}
        request = SummaryRequest(
            role="summarizer",
            route_id=self._route_id,
            messages=[system_msg, user_msg],
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
        # FatalError from the gateway typically means model_not_found —
        # the demo's hardcoded ``local.summary`` route_id won't match an
        # installed Ollama model on most machines. Fall back to the
        # structural-metadata path when policy allows (per QA P1 #1,
        # never extractive); otherwise propagate so downstream sees the
        # fail-closed reason.
        from errorta_council.gateway_local import FatalError
        try:
            result = await self._gateway.summarize(request)
        except FatalError as exc:
            if self._allow_extractive_fallback:
                content = self._structural_fallback(envelopes)
                return SummaryArtifact(
                    artifact_id="sa-" + uuid.uuid4().hex[:16],
                    content=content,
                    content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                    summary_mode="structural",
                    summarizer_route_id=self._route_id,
                    summarizer_version=self.version,
                    input_tokens=None, output_tokens=None,
                    source_hashes=source_hashes,
                )
            raise SummarizerUnavailable(
                f"summarizer unavailable ({exc}); fallback forbidden"
            ) from exc
        content, mode = self._enforce_isolation(
            result.content, envelopes, default_mode="abstractive",
        )
        return SummaryArtifact(
            artifact_id="sa-" + uuid.uuid4().hex[:16],
            content=content,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            summary_mode=mode,
            summarizer_route_id=self._route_id,
            summarizer_version=self.version,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            source_hashes=source_hashes,
        )

    def _enforce_isolation(
        self,
        produced: str,
        envelopes: list[SourceEnvelope],
        *,
        default_mode: str,
    ) -> tuple[str, str]:
        """F031-07 isolation backstop.

        If the produced summary contains a contiguous run of >= threshold
        characters that also appears in any source envelope content, the
        invariant-5 byte-isolation contract is at risk. Swap the
        ``produced`` content for a structural-metadata fallback (no
        source bytes) and label the artifact as ``structural``.

        Threshold = 0 disables the check entirely (test-only).
        """
        threshold = self._leak_threshold
        if threshold <= 0 or not produced:
            return produced, default_mode
        # Walk windows of `threshold` chars across the produced summary,
        # check each window against each envelope. Stop at first hit.
        # Complexity: O(len(produced) * sum(len(env.content))) worst case,
        # but the inner check is a fast C-level substring search.
        for start in range(0, max(0, len(produced) - threshold + 1)):
            window = produced[start:start + threshold]
            for env in envelopes:
                src = env.content or ""
                if not src:
                    continue
                if window in src:
                    return self._structural_fallback(envelopes), "structural"
        return produced, default_mode

    def _structural_fallback(self, envelopes: list[SourceEnvelope]) -> str:
        """Content-free metadata when extraction would have leaked source bytes.

        Carries class breakdown + envelope count so the downstream member
        knows the corpus was referenced (and what kinds of sources) even
        though no actual content surfaces in the payload.
        """
        if not envelopes:
            return "Summary unavailable; no source envelopes provided."
        classes = sorted({e.class_ for e in envelopes})
        n = len(envelopes)
        cls_list = ", ".join(classes) if classes else "unknown"
        return (
            f"Summary unavailable (corpus content not safely summarizable). "
            f"{n} source envelope(s) referenced; classes: {cls_list}."
        )

    def _extractive(self, envs: list[SourceEnvelope], max_output_tokens: int) -> str:
        chunks: list[str] = []
        budget = max_output_tokens
        for e in envs:
            first = e.content.split(".")[0].strip()
            if not first:
                continue
            tokens = max(1, len(first.split()))
            if tokens > budget:
                break
            chunks.append(first)
            budget -= tokens
        return ". ".join(chunks)


__all__ = ["SummaryPipeline", "SummaryArtifact", "SUMMARIZER_VERSION", "SummarizerUnavailable"]
