"""Prompt seed loader for the F-DEMO-01 benchmark harness.

A prompt has an ``id``, a primary ``text``, a ``paraphrase`` (used by the
F024 paraphrase-delta side-run), and an optional ``expected_topics`` list
that downstream graders may use for topical sanity checks.

The YAML loader validates structurally:
  * ids must be unique across the file
  * both ``text`` and ``paraphrase`` must be non-empty strings

Anything else is delegated to the harness layer — this module deliberately
stays a pure-logic data loader so it can be unit-tested without I/O mocks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class BenchmarkPrompt:
    """One seeded prompt in a benchmark run."""

    id: str
    text: str
    paraphrase: str
    expected_topics: list[str] = field(default_factory=list)


def _require_nonempty(value: Any, *, field_name: str, prompt_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"prompt '{prompt_id}': field '{field_name}' must be a non-empty string"
        )
    return value


def load_prompts_yaml(path: str | Path) -> list[BenchmarkPrompt]:
    """Load and validate a seed YAML file.

    Raises ``ValueError`` if the file shape is wrong, ids duplicate, or any
    entry has empty ``text`` or ``paraphrase``.
    """
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, list):
        raise ValueError(f"{p}: expected a YAML list at the top level")

    seen: set[str] = set()
    out: list[BenchmarkPrompt] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"{p}: entry {i} is not a mapping")
        prompt_id = entry.get("id")
        if not isinstance(prompt_id, str) or not prompt_id.strip():
            raise ValueError(f"{p}: entry {i} has missing/empty 'id'")
        if prompt_id in seen:
            raise ValueError(f"{p}: duplicate prompt id '{prompt_id}'")
        seen.add(prompt_id)

        text = _require_nonempty(entry.get("text"), field_name="text", prompt_id=prompt_id)
        paraphrase = _require_nonempty(
            entry.get("paraphrase"), field_name="paraphrase", prompt_id=prompt_id
        )
        topics_raw = entry.get("expected_topics") or []
        if not isinstance(topics_raw, list):
            raise ValueError(
                f"prompt '{prompt_id}': 'expected_topics' must be a list when present"
            )
        topics = [str(t) for t in topics_raw]

        out.append(
            BenchmarkPrompt(
                id=prompt_id,
                text=text,
                paraphrase=paraphrase,
                expected_topics=topics,
            )
        )

    return out
