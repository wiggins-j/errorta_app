"""F-WEDGE-DEEPEN-V1 — Judge replay across the grounding store.

Re-runs prior verdicts through the current pipeline and surfaces
score deltas. Reads verdicts.jsonl via :mod:`errorta_judge.metrics`
helpers; never mutates the existing log.

Public API:

* :class:`ReplayResult` — per-verdict comparison shape.
* :func:`list_verdicts_for_corpus` — newest-first verdict log filtered
  by ``corpus`` name (exact match) and ``accepted=False`` (dedup by id;
  acceptance follow-ups are skipped).
* :func:`replay_verdict` — re-runs a single entry against an injected
  pipeline returning a :class:`ReplayResult`.
* :func:`replay_corpus_stream` — async generator yielding
  :class:`ReplayResult` instances per replayed verdict. Respects
  ``ERRORTA_REPLAY_CONCURRENCY`` (default 2). Honors ``dry_run`` by
  emitting schema-only previews with no pipeline calls.

The Pipeline protocol from :mod:`errorta_query.pipeline` is reused
as-is; no new abstractions, no edits to the pipeline modules.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import os
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Optional

from errorta_judge import metrics as _metrics


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


@dataclass
class ReplayResult:
    """A single replayed verdict's before/after comparison."""

    prompt: str
    original_answer: str
    original_verdict: dict[str, Any]
    original_grounding_match: Optional[dict[str, Any]] = None
    replay_answer: str = ""
    replay_verdict: dict[str, Any] = field(default_factory=dict)
    replay_grounding_match: Optional[dict[str, Any]] = None
    score_delta: float = 0.0
    grounding_change: str = "unchanged"  # "added" | "removed" | "unchanged"
    occurred_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Rating -> numeric score (higher is better). Mirrors the simple
# heuristic the metrics summary already implies for pass/partial/fail.
_RATING_SCORE = {"pass": 1.0, "partial": 0.5, "fail": 0.0}


def _verdict_score(verdict: dict[str, Any] | None) -> float:
    if not isinstance(verdict, dict):
        return 0.0
    rating = str(verdict.get("rating") or "").lower()
    base = _RATING_SCORE.get(rating, 0.0)
    conf = verdict.get("confidence")
    try:
        conf_f = float(conf) if conf is not None else 0.5
    except (TypeError, ValueError):
        conf_f = 0.5
    # Weight rating heavily, nudge by confidence so two passes with
    # different certainty don't tie at exactly 1.0.
    return round(base * 0.8 + conf_f * 0.2, 4)


def list_verdicts_for_corpus(corpus_name: str) -> list[dict[str, Any]]:
    """Return non-accepted verdict entries filtered by exact corpus name.

    Dedupes by id (acceptance / supersede follow-up entries share the
    original event id; they are filtered out so each verdict surfaces
    exactly once). Newest-first order.
    """
    if not corpus_name:
        return []

    all_entries = list(_metrics._iter_entries())  # type: ignore[attr-defined]

    # Build set of ids that have an acceptance follow-up so we can
    # exclude those originals too — once accepted, a verdict is "done"
    # and shouldn't be replayed.
    accepted_ids: set[str] = set()
    for entry in all_entries:
        if entry.get("accepted") and entry.get("supersedes"):
            eid = entry.get("id")
            if eid:
                accepted_ids.add(eid)

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for entry in reversed(all_entries):
        eid = entry.get("id")
        if not eid or eid in seen:
            continue
        # Skip acceptance follow-up rows (they share an id with original).
        if entry.get("accepted") and entry.get("supersedes"):
            continue
        seen.add(eid)
        if eid in accepted_ids:
            continue
        if entry.get("corpus") != corpus_name:
            continue
        out.append(entry)
    return out


def replay_verdict(entry: dict[str, Any], pipeline: Any) -> ReplayResult:
    """Re-run an entry's prompt through ``pipeline`` and diff verdicts.

    The pipeline must satisfy :class:`errorta_query.pipeline.Pipeline`.
    Computes ``score_delta`` = replay_score - original_score (positive
    means improvement). The ``replay_grounding_match`` field is
    populated when the pipeline result carries grounding metadata; the
    diff against the original is summarised in ``grounding_change``.
    """
    prompt = entry.get("prompt") or ""
    original_verdict = entry.get("verdict") or {}
    original_answer = entry.get("answer") or ""
    original_grounding = entry.get("grounding_match")
    judge_model = entry.get("judge_model")

    result = pipeline.answer(
        prompt=prompt,
        corpus=entry.get("corpus") or "",
        judge=True,
        reground=True,
        model=judge_model,
    )

    # Adapter parity with the verdict route: prefer raw_verdict; fall
    # back to a typed Verdict's to_dict() when only that is available.
    raw = getattr(result, "raw_verdict", None)
    if raw is None and getattr(result, "verdict", None) is not None:
        try:
            raw = result.verdict.to_dict()
        except Exception:
            raw = None
    replay_verdict_dict: dict[str, Any] = raw if isinstance(raw, dict) else {}

    replay_answer = getattr(result, "answer", "") or ""
    replay_grounding = getattr(result, "grounding_match", None)
    if replay_grounding is not None and not isinstance(replay_grounding, dict):
        # Best-effort coerce; some adapters may return a dataclass.
        try:
            replay_grounding = asdict(replay_grounding)
        except TypeError:
            replay_grounding = None

    score_delta = round(
        _verdict_score(replay_verdict_dict) - _verdict_score(original_verdict),
        4,
    )

    had_orig = bool(original_grounding)
    had_replay = bool(replay_grounding)
    if had_orig and not had_replay:
        change = "removed"
    elif had_replay and not had_orig:
        change = "added"
    else:
        change = "unchanged"

    return ReplayResult(
        prompt=prompt,
        original_answer=original_answer,
        original_verdict=original_verdict,
        original_grounding_match=original_grounding
        if isinstance(original_grounding, dict)
        else None,
        replay_answer=replay_answer,
        replay_verdict=replay_verdict_dict,
        replay_grounding_match=replay_grounding,
        score_delta=score_delta,
        grounding_change=change,
        occurred_at=_now_iso(),
    )


def _dry_preview(entry: dict[str, Any]) -> ReplayResult:
    """Schema-shape preview for ``dry_run=True`` — no pipeline call."""
    original_verdict = entry.get("verdict") or {}
    original_grounding = entry.get("grounding_match")
    return ReplayResult(
        prompt=entry.get("prompt") or "",
        original_answer=entry.get("answer") or "",
        original_verdict=original_verdict,
        original_grounding_match=original_grounding
        if isinstance(original_grounding, dict)
        else None,
        replay_answer="",
        replay_verdict={},
        replay_grounding_match=None,
        score_delta=0.0,
        grounding_change="unchanged",
        occurred_at=_now_iso(),
    )


def _concurrency_cap() -> int:
    raw = os.environ.get("ERRORTA_REPLAY_CONCURRENCY")
    try:
        n = int(raw) if raw else 2
    except (TypeError, ValueError):
        n = 2
    return max(1, n)


async def replay_corpus_stream(
    corpus_name: str,
    pipeline: Any,
    *,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> AsyncIterator[ReplayResult]:
    """Async generator yielding :class:`ReplayResult` per replayed verdict.

    Honors ``ERRORTA_REPLAY_CONCURRENCY`` (default 2) when running
    non-dry replays through the pipeline. Dry-run mode skips the
    pipeline call entirely and yields schema-only previews so the UI
    can render its before-state without burning any model time.
    """
    entries = list_verdicts_for_corpus(corpus_name)
    if limit is not None and limit > 0:
        entries = entries[:limit]
    if not entries:
        return

    if dry_run:
        for entry in entries:
            yield _dry_preview(entry)
        return

    loop = asyncio.get_event_loop()
    sem = asyncio.Semaphore(_concurrency_cap())

    async def _run_one(entry: dict[str, Any]) -> ReplayResult:
        async with sem:
            return await loop.run_in_executor(
                None, lambda: replay_verdict(entry, pipeline)
            )

    # Preserve newest-first order in the stream; bounded concurrency
    # is achieved via the semaphore inside each task.
    tasks = [asyncio.create_task(_run_one(e)) for e in entries]
    for task in tasks:
        result = await task
        yield result
