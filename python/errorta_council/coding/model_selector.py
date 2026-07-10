"""Pure, deterministic F129 route selection."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .model_catalog import ModelCatalogEntry
from .model_tier import tier_rank

# F129/F135 shared performance thresholds. These are the SINGLE source of truth
# for "has a route done well enough?" — the selector acts on them (demotion) and
# the F135 learning projection labels standings from the same constants, so the
# explanation can never drift from the behavior.
MIN_ATTEMPTS_FOR_SIGNAL = 5
DEMOTION_ACCEPTED_RATE = 0.60
PREFERRED_ACCEPTED_RATE = 0.80  # presentation-only (F135 "preferred" standing)


@dataclass(frozen=True)
class Selection:
    route_id: str
    entry: ModelCatalogEntry
    rationale: str


@dataclass(frozen=True)
class NoCapableModel:
    reason: str
    considered: tuple[str, ...] = ()


def _effective_rank(
    entry: ModelCatalogEntry,
    task_type: str,
    difficulty: str,
    digest: dict[str, Any] | None,
) -> tuple[int, float]:
    base = tier_rank(entry.capability_tier)
    stats = (digest or {}).get(entry.route_id, {})
    exact = stats.get(f"{task_type}:{difficulty}", stats) if isinstance(stats, dict) else {}
    attempts = int(exact.get("attempts", 0)) if isinstance(exact, dict) else 0
    rate = float(exact.get("accepted_rate", 1.0)) if isinstance(exact, dict) else 1.0
    if attempts >= MIN_ATTEMPTS_FOR_SIGNAL and rate < DEMOTION_ACCEPTED_RATE:
        base = max(0, base - 1)
    return base, 1.0 - rate


def select(
    pool: list[str],
    available: set[str],
    catalog: dict[str, ModelCatalogEntry],
    difficulty: str,
    *,
    task_type: str = "implementation",
    corpus_digest: dict[str, Any] | None = None,
    minimum_rank_exclusive: int | None = None,
) -> Selection | NoCapableModel:
    if not pool:
        return NoCapableModel("empty_pool")
    requested_rank = tier_rank(difficulty)
    candidates: list[tuple[tuple[Any, ...], ModelCatalogEntry]] = []
    for route_id in dict.fromkeys(pool):
        if route_id not in available:
            continue
        entry = catalog.get(route_id)
        if entry is None:
            continue
        effective_rank, corpus_penalty = _effective_rank(
            entry, task_type, difficulty, corpus_digest,
        )
        if effective_rank < requested_rank:
            continue
        if minimum_rank_exclusive is not None and effective_rank <= minimum_rank_exclusive:
            continue
        key = (
            entry.cost_tier,
            effective_rank,
            corpus_penalty,
            entry.size_rank,
            entry.speed_rank,
            entry.route_id,
        )
        candidates.append((key, entry))
    if not candidates:
        reason = "unavailable" if not available.intersection(pool) else "no_capable_model"
        return NoCapableModel(reason, tuple(pool))
    _, chosen = min(candidates, key=lambda item: item[0])
    return Selection(
        route_id=chosen.route_id,
        entry=chosen,
        rationale=(
            f"lowest cost tier {chosen.cost_tier} among available routes at or above "
            f"{difficulty} capability"
        ),
    )


__all__ = ["NoCapableModel", "Selection", "select"]
