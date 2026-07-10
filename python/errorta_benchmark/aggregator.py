"""Aggregator — turns a list of RecordedVerdict into AggregationResult.

Pure-logic; safe to test without any I/O. Aggregates:

  * ``median_score``  — median over recorded scores (pass=1, partial/uncertain=0.5,
    fail/error=0)
  * ``pass_rate``     — fraction with rating == "pass"
  * ``rating_counts`` — Counter-style dict
  * ``before_after_delta`` — pass_rate_after − pass_rate_before, computed
    only over the *intersection* of prompt ids present in both inputs.
    Unmatched ids are excluded from the delta.
  * ``F024_paraphrase_delta`` — pass_rate over paraphrase re-runs minus
    pass_rate over their primary counterparts, over matched ids only. Set
    to ``None`` when no ``is_paraphrase_re_run=True`` entries exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .runner import RecordedVerdict


@dataclass(frozen=True)
class AggregationResult:
    total: int
    median_score: float
    pass_rate: float
    rating_counts: dict[str, int] = field(default_factory=dict)
    before_after_delta: Optional[float] = None
    F024_paraphrase_delta: Optional[float] = None
    matched_prompt_ids: list[str] = field(default_factory=list)
    # BENCH-WEDGE: F024 grounding amplification metrics. Computed over the
    # subset of paraphrase re-runs whose verdict response carried
    # ``grounding_match.kind == "similar"`` — i.e. the wedge actually fired.
    #
    #   f024_similar_match_count       — number of paraphrase verdicts with
    #                                    a similar grounding match
    #   f024_similar_match_rate        — count / total paraphrase verdicts
    #   f024_similar_match_score_delta — mean recorded score over the
    #                                    similar-match subset minus mean score
    #                                    over the primary counterparts
    f024_similar_match_count: int = 0
    f024_similar_match_rate: Optional[float] = None
    f024_similar_match_score_delta: Optional[float] = None
    f024_similar_match_mean_similarity: Optional[float] = None


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return float(s[mid])
    return float((s[mid - 1] + s[mid]) / 2.0)


def _pass_rate(items: Iterable[RecordedVerdict]) -> float:
    items = list(items)
    if not items:
        return 0.0
    passes = sum(1 for v in items if v.rating == "pass")
    return passes / len(items)


def _mean_score(items: Iterable[RecordedVerdict]) -> float:
    items = list(items)
    if not items:
        return 0.0
    return sum(v.score for v in items) / len(items)


class BenchmarkAggregator:
    """Stateless aggregator. Methods are pure functions over inputs."""

    def aggregate(
        self,
        verdicts: list[RecordedVerdict],
        *,
        before: Optional[list[RecordedVerdict]] = None,
        after: Optional[list[RecordedVerdict]] = None,
    ) -> AggregationResult:
        if not verdicts and not before and not after:
            return AggregationResult(
                total=0,
                median_score=0.0,
                pass_rate=0.0,
                rating_counts={},
                before_after_delta=None,
                F024_paraphrase_delta=None,
                matched_prompt_ids=[],
                f024_similar_match_count=0,
                f024_similar_match_rate=None,
                f024_similar_match_score_delta=None,
                f024_similar_match_mean_similarity=None,
            )

        primary = [v for v in verdicts if not v.is_paraphrase_re_run]
        paraphrase = [v for v in verdicts if v.is_paraphrase_re_run]

        rating_counts: dict[str, int] = {}
        for v in verdicts:
            rating_counts[v.rating] = rating_counts.get(v.rating, 0) + 1

        median_score = _median([v.score for v in verdicts])
        pass_rate = _pass_rate(verdicts)

        # F024 paraphrase delta — only computed when paraphrase entries exist,
        # and only over the intersection of prompt ids.
        f024_delta: Optional[float] = None
        if paraphrase:
            primary_by_id = {v.prompt_id: v for v in primary}
            para_by_id = {v.prompt_id: v for v in paraphrase}
            matched = sorted(set(primary_by_id) & set(para_by_id))
            if matched:
                p_before = _pass_rate(primary_by_id[i] for i in matched)
                p_after = _pass_rate(para_by_id[i] for i in matched)
                f024_delta = p_after - p_before

        # BENCH-WEDGE: F024 grounding amplification metrics.
        f024_sim_count = 0
        f024_sim_rate: Optional[float] = None
        f024_sim_delta: Optional[float] = None
        f024_sim_mean: Optional[float] = None
        if paraphrase:
            similar = [
                v for v in paraphrase
                if (v.grounding_match_kind or "").lower() == "similar"
            ]
            f024_sim_count = len(similar)
            f024_sim_rate = f024_sim_count / len(paraphrase)
            sims = [
                v.grounding_match_similarity for v in similar
                if v.grounding_match_similarity is not None
            ]
            if sims:
                f024_sim_mean = float(sum(sims) / len(sims))
            if similar:
                primary_by_id_for_sim = {v.prompt_id: v for v in primary}
                matched_sim = [
                    v for v in similar if v.prompt_id in primary_by_id_for_sim
                ]
                if matched_sim:
                    score_after = _mean_score(matched_sim)
                    score_before = _mean_score(
                        primary_by_id_for_sim[v.prompt_id] for v in matched_sim
                    )
                    f024_sim_delta = score_after - score_before

        # Before/after delta — also over matched prompt ids only.
        ba_delta: Optional[float] = None
        matched_ids: list[str] = []
        if before is not None and after is not None:
            before_by_id = {v.prompt_id: v for v in before if not v.is_paraphrase_re_run}
            after_by_id = {v.prompt_id: v for v in after if not v.is_paraphrase_re_run}
            matched_ids = sorted(set(before_by_id) & set(after_by_id))
            if matched_ids:
                p_before = _pass_rate(before_by_id[i] for i in matched_ids)
                p_after = _pass_rate(after_by_id[i] for i in matched_ids)
                ba_delta = p_after - p_before

        return AggregationResult(
            total=len(verdicts),
            median_score=median_score,
            pass_rate=pass_rate,
            rating_counts=rating_counts,
            before_after_delta=ba_delta,
            F024_paraphrase_delta=f024_delta,
            matched_prompt_ids=matched_ids,
            f024_similar_match_count=f024_sim_count,
            f024_similar_match_rate=f024_sim_rate,
            f024_similar_match_score_delta=f024_sim_delta,
            f024_similar_match_mean_similarity=f024_sim_mean,
        )
