"""Versioned F036 deliberation style prompts."""

TELEGRAPHIC_V1_BASE = (
    "Deliberation style: telegraph. No preamble. Do not restate other "
    "members' points; reference them by member id. Fragments acceptable. "
    "State only: your position, new evidence or reasoning, disagreements "
    "with reasons, changed views. Plain English; no invented abbreviations."
)

TELEGRAPHIC_V1_WITH_CITES = (
    TELEGRAPHIC_V1_BASE + " Cite sources as [c:ID] when citation ids are provided."
)

STYLE_PROMPT_VERSION = "telegraphic_v1"

__all__ = [
    "STYLE_PROMPT_VERSION",
    "TELEGRAPHIC_V1_BASE",
    "TELEGRAPHIC_V1_WITH_CITES",
]
