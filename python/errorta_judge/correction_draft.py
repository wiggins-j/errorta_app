"""Propose initial correction text for the user to edit before accepting.

v0.1 keeps this simple: a templated draft built from the answer + verdict.
Future versions may ask a small LLM to summarize the gap.
"""
from __future__ import annotations

from typing import Any


def draft_correction(answer: str, verdict: dict[str, Any]) -> str:
    rating = (verdict.get("rating") or "").lower()
    reason = (verdict.get("reason") or "").strip()
    tags = verdict.get("failure_tags") or []

    if rating == "pass":
        # Even on pass, the user may want to add a clarifying note.
        return (answer or "").strip()

    header_bits: list[str] = []
    if reason:
        header_bits.append(f"Judge said: {reason}")
    if tags:
        header_bits.append(f"Tags: {', '.join(tags)}")
    header = "\n".join(header_bits)

    body = (answer or "").strip()
    if not body:
        return header or "Add the correct answer here."

    if header:
        return f"{header}\n\nCorrected answer:\n{body}"
    return body
