"""Spec 08 — task-creation dedupe.

The observed failure: 130 todo tasks across ~35 distinct titles, all restating
2–3 real jobs. Nothing anywhere compared a planned task against the backlog —
``store.add_task`` minted a fresh uuid and blind-appended. The PM narrated the
duplication and then created another duplicate.

This module is the *pure* half of the fix: given a planned task and an index of
the currently **open** backlog, decide whether the planned task is materially the
same job. Callers (``runner._materialize_pm_tasks`` and
``control_actions.create_task``) own the side effects — skipping the create,
recording an auditable decision, and keeping ``depends_on`` resolvable.

Conservative by construction. A false positive silently drops real work, which is
strictly worse than a duplicate, so every rule requires a strong signal and
declared target paths act as a *disambiguator*: two tasks that each name a
different set of files are different jobs even when their titles agree.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from . import paths as _paths

# A task only blocks a new one while it is still live. Re-doing finished work is
# legitimate (a regression), so `done`/`dropped`/`blocked` never suppress a create.
OPEN_STATES = frozenset({"todo", "doing"})

# Leading verbs the PM swaps freely while restating the same job
# ("Fix the harness" / "Create the harness" / "Consolidate the harness").
_FILLER_VERBS = frozenset({
    "fix", "create", "consolidate", "add", "update", "implement",
})

_PUNCT_RE = re.compile(r"[^\w\s]+")

# Jaccard over the filler-stripped token sets. Deliberately high: 0.8 means a
# 5-token title may differ by at most one token. Lower thresholds start
# collapsing "Add X to the parser" / "Add Y to the parser".
TITLE_SIMILARITY_THRESHOLD = 0.8

# Rule (b) — identical target paths — corroborates a *weaker* title match rather
# than standing alone. Standing alone it is a false-positive machine: "update
# pricing" / "cover pricing" / "document pricing" all name ``pricing.py`` and are
# all role dev, yet they are three genuinely different jobs (the existing
# Spec 09 hot-file tests plan exactly that batch). Requiring a majority token
# overlap keeps rule (b) useful — it catches a reworded restatement of the same
# job at a bar rule (a) would miss — without collapsing distinct work on a
# shared file. See the module docstring: under-dedupe beats over-dedupe.
PATH_RULE_TITLE_FLOOR = 0.6

RULE_TITLE = "normalized_title"
RULE_PATHS = "target_paths"


@dataclass(frozen=True)
class OpenTask:
    """The comparison-relevant projection of one open backlog task."""

    task_id: str
    title: str
    role: str
    tokens: frozenset[str]
    paths: frozenset[str]


@dataclass(frozen=True)
class DuplicateMatch:
    """Why a planned task was rejected, in renderable form."""

    task_id: str
    title: str
    rule: str
    similarity: float

    def rationale(self, planned_title: str) -> str:
        if self.rule == RULE_PATHS:
            why = ("identical declared target paths and role, title Jaccard "
                   f"{self.similarity:.2f}")
        else:
            why = f"normalized-title Jaccard {self.similarity:.2f}"
        return (f"planned {planned_title!r} duplicates open task {self.task_id} "
                f"({self.title!r}) — {why}")


def normalized_tokens(*parts: str) -> frozenset[str]:
    """Lowercase, strip punctuation, collapse whitespace, drop leading filler
    verbs. Falls back to the un-stripped tokens when a title is *only* filler
    verbs, so "Fix" and "Update" don't normalize to the same empty set."""
    raw = [t for t in _PUNCT_RE.sub(" ", " ".join(
        str(p or "") for p in parts).lower()).split() if t]
    stripped = list(raw)
    while stripped and stripped[0] in _FILLER_VERBS:
        stripped.pop(0)
    return frozenset(stripped or raw)


def title_similarity(a: Iterable[str], b: Iterable[str]) -> float:
    """Jaccard over token sets. An empty side carries no signal → 0.0."""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def task_paths(task: Any) -> frozenset[str]:
    """Declared ``target_files`` unioned with paths inferred from title/detail
    prose — the same view the hot-file serializer uses."""
    return frozenset(_paths.task_touched_paths(task))


def normalized_target_paths(declared: Iterable[str] | None) -> frozenset[str]:
    """Canonicalize an explicitly declared ``target_files`` list so it compares
    equal to the paths projected out of an existing task."""
    return frozenset(
        _paths.normalize_path(p) for p in (declared or []) if str(p or "").strip())


def build_open_index(tasks: Iterable[Any]) -> list[OpenTask]:
    """Project the OPEN tasks of a backlog into comparison form. Closed tasks are
    dropped here, which is what makes "duplicate of a done task" legal."""
    index: list[OpenTask] = []
    for task in tasks:
        if str(getattr(task, "state", "") or "") not in OPEN_STATES:
            continue
        index.append(OpenTask(
            task_id=str(getattr(task, "task_id", "") or ""),
            title=str(getattr(task, "title", "") or ""),
            role=str(getattr(task, "role", "") or ""),
            tokens=normalized_tokens(getattr(task, "title", "")),
            paths=task_paths(task),
        ))
    return index


def index_entry(*, task_id: str, title: str, role: str,
                paths: Iterable[str]) -> OpenTask:
    """Build an entry for a task just created in this batch, so a second
    identical proposal in the SAME plan is caught too."""
    return OpenTask(task_id=str(task_id), title=str(title), role=str(role),
                    tokens=normalized_tokens(title), paths=frozenset(paths))


def _paths_disagree(a: frozenset[str], b: frozenset[str]) -> bool:
    """Both sides named files and they named *different* files. That is positive
    evidence of two different jobs — it vetoes the title rule, which is how
    "same title, different target file" survives as two tasks."""
    return bool(a) and bool(b) and a != b


def find_duplicate(index: Iterable[OpenTask], *, title: str, role: str,
                   paths: Iterable[str]) -> DuplicateMatch | None:
    """The first open task the planned one materially duplicates, else ``None``.

    Rule (a) normalized title: Jaccard ≥ :data:`TITLE_SIMILARITY_THRESHOLD` over
    filler-stripped token sets, unless the two tasks declare different non-empty
    path sets.

    Rule (b) target paths: identical NON-EMPTY path sets, the same role, and a
    majority title overlap (:data:`PATH_RULE_TITLE_FLOOR`) — see that constant
    for why identical paths alone are not sufficient evidence.
    """
    planned_tokens = normalized_tokens(title)
    planned_paths = frozenset(paths)
    planned_role = str(role or "")
    for entry in index:
        similarity = title_similarity(planned_tokens, entry.tokens)
        same_target = (bool(planned_paths) and planned_paths == entry.paths
                       and planned_role == entry.role)
        if same_target and similarity >= PATH_RULE_TITLE_FLOOR:
            return DuplicateMatch(entry.task_id, entry.title, RULE_PATHS,
                                  similarity)
        # Different files named on both sides is positive evidence of two
        # different jobs; it vetoes the title-only rule.
        if _paths_disagree(planned_paths, entry.paths):
            continue
        if similarity >= TITLE_SIMILARITY_THRESHOLD:
            return DuplicateMatch(entry.task_id, entry.title, RULE_TITLE,
                                  similarity)
    return None
