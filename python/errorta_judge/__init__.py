"""errorta_judge — F001 judge/grounding domain package.

Provides defensive wrappers around aiar's judge/grounding substrate:
- schema_guard: validate/repair LLM-judge Verdict JSON
- metrics: append-only verdict log + roll-ups
- latency: stopwatch helper
- correction_draft: propose initial correction text for the user to edit
"""

__all__ = [
    "metrics",
    "schema_guard",
    "latency",
    "correction_draft",
]
