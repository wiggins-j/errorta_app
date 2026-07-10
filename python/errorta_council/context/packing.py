"""Deterministic token-budget packer.

Priority:
  task_instructions → user_prompt → grounding_hint → retrieved_snippet
  → tool_result_ref → style_instructions → dialect_instructions
  → citation_index → recent tool/child output → transcript → summary → metadata

user_prompt is the question; it must outrank every optional F036
efficiency block (style/dialect/citations) because the cascade drops
all lower-priority blocks once one misses the budget. QA review
2026-06-12 caught the prior ordering, which let a fat dialect_instructions
silently drop the user's question.

Records omitted blocks with stable reason strings ('token_cap',
'policy_block', 'redaction_unavailable', 'summary_stale').
Deterministic — repeated calls with identical input yield identical output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .tokens import HeuristicEstimator, TokenEstimator, content_kind_for_class

_PRIORITY = (
    "task_instructions",
    "user_prompt",
    # F049: a live user interjection is authoritative direction — it must
    # outrank every member/transcript block and never be dropped, so it sits
    # just below the prompt and above all optional + transcript content.
    "user_interjection",
    "grounding_hint",
    "retrieved_snippet",
    "tool_result_ref",
    "style_instructions",
    "dialect_instructions",
    "citation_index",
    "tool_result",
    "child_run_summary",
    "transcript_summary",
    "transcript",
    "transcript_event",
    "summary",
    "metadata",
)
_PRIORITY_RANK = {name: i for i, name in enumerate(_PRIORITY)}


@dataclass(frozen=True)
class PackedContext:
    kept: list[dict[str, Any]] = field(default_factory=list)
    omitted: list[dict[str, Any]] = field(default_factory=list)
    total_tokens: int = 0


class TokenPacker:
    def __init__(
        self,
        *,
        max_input_tokens: int,
        estimator: TokenEstimator | None = None,
    ) -> None:
        self._budget = int(max_input_tokens)
        self._estimator = estimator or HeuristicEstimator()

    @property
    def estimator_method(self) -> str:
        return getattr(self._estimator, "method", "unknown")

    @property
    def calibration_factor(self) -> float:
        return float(getattr(self._estimator, "calibration_factor", 1.0))

    def pack(self, blocks: list[dict[str, Any]]) -> PackedContext:
        sorted_blocks = sorted(
            enumerate(blocks),
            key=lambda iv: (_PRIORITY_RANK.get(iv[1]["class_"], 99), iv[0]),
        )
        kept: list[dict[str, Any]] = []
        omitted: list[dict[str, Any]] = []
        remaining = self._budget
        # Cascade: once a higher-priority block can't fit, every lower-priority
        # block is also omitted (deterministic, matches F031-05 packing semantics).
        cap_reached = False
        for _, block in sorted_blocks:
            block_with_tokens = dict(block)
            if not block_with_tokens.get("tokens"):
                block_with_tokens["tokens"] = self._estimator.estimate(
                    str(block_with_tokens.get("content") or ""),
                    content_kind=content_kind_for_class(
                        str(block_with_tokens.get("class_") or "")
                    ),
                )
            t = int(block_with_tokens.get("tokens") or 0)
            if not cap_reached and t <= remaining:
                kept.append(block_with_tokens)
                remaining -= t
            else:
                cap_reached = True
                omitted.append({
                    "class_": block_with_tokens["class_"],
                    "content_sha256": block_with_tokens.get("content_sha256"),
                    "reason": "token_cap",
                    "tokens": t,
                })
        return PackedContext(
            kept=kept, omitted=omitted, total_tokens=self._budget - remaining
        )


__all__ = ["TokenPacker", "PackedContext"]
