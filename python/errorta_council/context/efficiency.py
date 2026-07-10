"""F036 context-efficiency config resolver."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TranscriptCompactionConfig:
    enabled: bool = False
    full_rounds_window: int = 2
    segment_size_rounds: int = 4
    on_summary_unavailable: str = "structural"


@dataclass(frozen=True)
class ToolResultCompactionConfig:
    enabled: bool = False
    recent_results_window: int = 1
    max_raw_tool_result_tokens: int = 2048


@dataclass(frozen=True)
class ContextEfficiencyConfig:
    deliberation_style: str = "natural"
    intermediate_max_output_tokens: int | None = None
    deliberation_dialect: str = "prose"
    citation_references: bool = False
    transcript_compaction: TranscriptCompactionConfig = field(
        default_factory=TranscriptCompactionConfig
    )
    tool_result_compaction: ToolResultCompactionConfig = field(
        default_factory=ToolResultCompactionConfig
    )
    prompt_cache_hints: bool = False


def resolve_context_efficiency(snapshot: dict[str, Any]) -> ContextEfficiencyConfig:
    room = dict(snapshot.get("room") or {})
    raw = dict(room.get("context_efficiency") or {})
    compaction_raw = dict(raw.get("transcript_compaction") or {})
    tool_compaction_raw = dict(raw.get("tool_result_compaction") or {})
    recent_results_window = (
        _non_negative_int(tool_compaction_raw.get("recent_results_window"))
        if tool_compaction_raw.get("recent_results_window") is not None
        else 1
    )
    if recent_results_window is None:
        recent_results_window = 1
    return ContextEfficiencyConfig(
        deliberation_style=_choice(
            raw.get("deliberation_style"),
            {"natural", "telegraphic"},
            "natural",
        ),
        intermediate_max_output_tokens=_positive_int(raw.get("intermediate_max_output_tokens")),
        deliberation_dialect=_choice(
            raw.get("deliberation_dialect"),
            {"prose", "digest_v1"},
            "prose",
        ),
        citation_references=bool(raw.get("citation_references", False)),
        transcript_compaction=TranscriptCompactionConfig(
            enabled=bool(compaction_raw.get("enabled", False)),
            full_rounds_window=_positive_int(compaction_raw.get("full_rounds_window")) or 2,
            segment_size_rounds=_positive_int(compaction_raw.get("segment_size_rounds")) or 4,
            on_summary_unavailable=_choice(
                compaction_raw.get("on_summary_unavailable"),
                {"structural", "verbatim"},
                "structural",
            ),
        ),
        tool_result_compaction=ToolResultCompactionConfig(
            enabled=bool(tool_compaction_raw.get("enabled", False)),
            recent_results_window=recent_results_window,
            max_raw_tool_result_tokens=(
                _positive_int(tool_compaction_raw.get("max_raw_tool_result_tokens"))
                or 2048
            ),
        ),
        prompt_cache_hints=bool(raw.get("prompt_cache_hints", False)),
    )


def _choice(value: object, allowed: set[str], default: str) -> str:
    s = str(value or default)
    return s if s in allowed else default


def _positive_int(value: object) -> int | None:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _non_negative_int(value: object) -> int | None:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


__all__ = [
    "ContextEfficiencyConfig",
    "ToolResultCompactionConfig",
    "TranscriptCompactionConfig",
    "resolve_context_efficiency",
]
