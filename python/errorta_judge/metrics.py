"""Append-only verdict log under ~/.errorta/verdicts.jsonl.

Each line is a JSON object:
    {
      "id": str,                # uuid for this verdict event
      "prompt": str,
      "answer": str,
      "verdict": {rating, reason, failure_tags, confidence, latency_ms},
      "judge_model": str | None,
      "accepted": bool,         # set true when the user accepts a correction
      "correction": str | None,
      "created_at": iso8601 str
    }

The roll-ups (pass rate, 7d trend, most-corrected prompts) are computed
on read; nothing fancy. For v0.1 this stays cheap and rebuilds from
the log on every /judge/metrics call.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

_LOCK = threading.Lock()


def _home() -> Path:
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def log_path() -> Path:
    """Verdict log path. Routes through ``errorta_app.paths.verdicts_log_path()``
    so the consolidated F-INFRA-12 ERRORTA_HOME env var is honored."""
    from errorta_app.paths import verdicts_log_path
    return verdicts_log_path()


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def record_verdict(
    prompt: str,
    answer: str,
    verdict: dict[str, Any],
    judge_model: str | None,
    *,
    prompt_signature: str | None = None,
    corpus: str | None = None,
) -> str:
    """Append a verdict event to the log; return the event id.

    ``prompt_signature`` is keyword-only and additive so legacy callers
    remain valid. When supplied it is persisted into the log entry to
    avoid recomputation in :func:`list_prior_verdicts`.

    ``corpus`` is keyword-only and additive. When supplied it is
    persisted so the replay subsystem (F-WEDGE-DEEPEN-V1) can filter
    verdicts per-corpus. Old log lines without the field load as
    ``corpus=None`` without error.
    """
    eid = uuid.uuid4().hex
    entry = {
        "id": eid,
        "prompt": prompt,
        "answer": answer,
        "verdict": verdict,
        "judge_model": judge_model,
        "accepted": False,
        "correction": None,
        "created_at": _now_iso(),
        "prompt_signature": prompt_signature,
        "corpus": corpus,
    }
    _append(entry)
    return eid


def list_prior_verdicts(
    signature: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return prior verdicts for a given prompt signature, newest-first.

    Behavior:
      * Iterates the log newest-first.
      * Collapses follow-up events (acceptance / supersede) by ``id`` so a
        given verdict event only contributes once.
      * Excludes the most-recent matching verdict — that is the "current"
        verdict and never returned as a prior.
      * Matches on the persisted ``prompt_signature`` when present; for
        legacy lines that lack the field, falls back to recomputing the
        signature from ``entry['prompt']``.
      * Returns at most ``limit`` priors. Each prior payload exposes
        ``verdict``, ``judge_model``, ``created_at``.
    """
    from errorta_query.signature import prompt_signature as _sig

    if not signature:
        return []

    # Collect entries newest-first, de-duped by event id. Acceptance /
    # supersede follow-ups (``accepted=True`` and ``supersedes`` set)
    # share the original event's id; they're filtered out here so the
    # original verdict event remains the canonical row per id.
    all_entries = list(_iter_entries())
    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for entry in reversed(all_entries):
        if entry.get("accepted") and entry.get("supersedes"):
            continue
        eid = entry.get("id")
        if not eid or eid in seen_ids:
            continue
        seen_ids.add(eid)
        deduped.append(entry)

    matching: list[dict[str, Any]] = []
    for entry in deduped:
        entry_sig = entry.get("prompt_signature")
        if not entry_sig:
            entry_sig = _sig(entry.get("prompt") or "")
        if entry_sig != signature:
            continue
        matching.append(entry)

    # First is the current verdict; priors are everything after.
    priors = matching[1:]
    out: list[dict[str, Any]] = []
    for p in priors[:limit]:
        out.append(
            {
                "verdict": p.get("verdict"),
                "judge_model": p.get("judge_model"),
                "created_at": p.get("created_at"),
            }
        )
    return out


def record_acceptance(
    event_id: str,
    correction: str | None,
) -> dict[str, Any] | None:
    """Mark a prior verdict accepted by writing a follow-up event.

    We don't mutate prior lines (append-only) — instead append a new
    record that supersedes the original on read.
    """
    prior = _find(event_id)
    if prior is None:
        return None
    entry = {
        "id": event_id,
        "prompt": prior.get("prompt", ""),
        "answer": prior.get("answer", ""),
        "verdict": prior.get("verdict"),
        "judge_model": prior.get("judge_model"),
        "accepted": True,
        "correction": correction,
        "created_at": _now_iso(),
        "supersedes": prior.get("created_at"),
        "prompt_signature": prior.get("prompt_signature"),
        "corpus": prior.get("corpus"),
    }
    _append(entry)
    return entry


def _append(entry: dict[str, Any]) -> None:
    path = log_path()
    line = json.dumps(entry, ensure_ascii=False)
    with _LOCK:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def _iter_entries() -> Iterable[dict[str, Any]]:
    path = log_path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except (ValueError, TypeError):
                continue
    return out


def _find(event_id: str) -> dict[str, Any] | None:
    # Last write wins.
    found: dict[str, Any] | None = None
    for entry in _iter_entries():
        if entry.get("id") == event_id:
            found = entry
    return found


def find_accepted_correction(prompt: str) -> str | None:
    """Return the most recently accepted correction for an exact prompt match."""
    latest: tuple[str, str] | None = None  # (created_at, correction)
    for entry in _iter_entries():
        if not entry.get("accepted"):
            continue
        if entry.get("prompt") != prompt:
            continue
        correction = entry.get("correction")
        if not correction:
            continue
        created = entry.get("created_at") or ""
        if latest is None or created > latest[0]:
            latest = (created, correction)
    return latest[1] if latest else None


def _reason_snippet(reason: Any, limit: int = 120) -> str:
    """Return a short reason snippet suitable for tooltip display."""
    if not isinstance(reason, str):
        return ""
    text = reason.strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# Fixed-order latency bucket definitions for the metrics histogram.
# Each entry is (label, lower_inclusive_ms, upper_exclusive_ms_or_None).
_LATENCY_BUCKETS: list[tuple[str, float, float | None]] = [
    ("0-250", 0.0, 250.0),
    ("250-500", 250.0, 500.0),
    ("500-750", 500.0, 750.0),
    ("750-1000", 750.0, 1000.0),
    ("1000-2000", 1000.0, 2000.0),
    ("2000+", 2000.0, None),
]


def _percentile_nearest_rank(sorted_vals: list[float], pct: float) -> float | None:
    """Nearest-rank percentile over a pre-sorted list of values.

    Returns ``None`` when the list is empty. Uses the standard nearest-rank
    definition: rank = ceil(pct/100 * N), clamped to [1, N].
    """
    n = len(sorted_vals)
    if n == 0:
        return None
    import math

    rank = max(1, min(n, math.ceil(pct / 100.0 * n)))
    return float(sorted_vals[rank - 1])


def _latency_histogram(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Build the latency histogram + p50/p95/p99 from verdict entries.

    Entries lacking a numeric ``verdict.latency_ms`` are skipped without
    raising. Returns shape:

        {
          "buckets": [{"label": str, "count": int}, ...],  # exactly 6
          "p50_ms": float | None,
          "p95_ms": float | None,
          "p99_ms": float | None,
        }
    """
    samples: list[float] = []
    counts = [0] * len(_LATENCY_BUCKETS)
    for entry in entries:
        verdict = entry.get("verdict") or {}
        raw = verdict.get("latency_ms")
        if raw is None:
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if v < 0:
            continue
        samples.append(v)
        for i, (_label, lo, hi) in enumerate(_LATENCY_BUCKETS):
            if hi is None:
                if v >= lo:
                    counts[i] += 1
                    break
            elif lo <= v < hi:
                counts[i] += 1
                break

    samples.sort()
    return {
        "buckets": [
            {"label": label, "count": counts[i]}
            for i, (label, _lo, _hi) in enumerate(_LATENCY_BUCKETS)
        ],
        "p50_ms": _percentile_nearest_rank(samples, 50),
        "p95_ms": _percentile_nearest_rank(samples, 95),
        "p99_ms": _percentile_nearest_rank(samples, 99),
    }


def summary(now: _dt.datetime | None = None) -> dict[str, Any]:
    """Pass rate (all-time + last 7d) + most-corrected prompts."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(days=7)

    # Collect ALL raw entries so we can build verdict timelines per prompt
    # while still deduping by id for the headline pass-rate metrics.
    all_entries = list(_iter_entries())

    # Group entries by id; keep the most recent per id.
    by_id: dict[str, dict[str, Any]] = {}
    for entry in all_entries:
        eid = entry.get("id")
        if not eid:
            continue
        prior = by_id.get(eid)
        if prior is None or (entry.get("created_at") or "") > (prior.get("created_at") or ""):
            by_id[eid] = entry

    total = 0
    passes = 0
    total_7d = 0
    passes_7d = 0
    corrected_counts: Counter[str] = Counter()
    rating_counts: Counter[str] = Counter()

    trend_buckets: dict[str, dict[str, int]] = {}
    for i in range(7):
        d = (now - _dt.timedelta(days=i)).date().isoformat()
        trend_buckets[d] = {"total": 0, "pass": 0}

    for entry in by_id.values():
        verdict = entry.get("verdict") or {}
        rating = (verdict.get("rating") or "").lower()
        rating_counts[rating or "unknown"] += 1
        total += 1
        is_pass = rating == "pass"
        if is_pass:
            passes += 1
        created_raw = entry.get("created_at") or ""
        try:
            created = _dt.datetime.fromisoformat(created_raw)
        except ValueError:
            created = None
        if created is not None and created >= cutoff:
            total_7d += 1
            if is_pass:
                passes_7d += 1
            day = created.date().isoformat()
            if day in trend_buckets:
                trend_buckets[day]["total"] += 1
                if is_pass:
                    trend_buckets[day]["pass"] += 1
        if entry.get("accepted") and entry.get("correction"):
            prompt = entry.get("prompt") or ""
            if prompt:
                corrected_counts[prompt] += 1

    def _rate(num: int, den: int) -> float | None:
        if den == 0:
            return None
        return round(num / den, 4)

    trend = [
        {
            "date": d,
            "total": trend_buckets[d]["total"],
            "pass": trend_buckets[d]["pass"],
            "pass_rate": _rate(trend_buckets[d]["pass"], trend_buckets[d]["total"]),
        }
        for d in sorted(trend_buckets.keys())
    ]

    # Build per-prompt verdict timelines from the raw log, oldest-first,
    # deduped by event id (so acceptance follow-ups don't double-count).
    from errorta_query.signature import prompt_signature as _sig

    seen_for_timeline: set[str] = set()
    timeline_by_prompt: dict[str, list[dict[str, Any]]] = {}
    signature_by_prompt: dict[str, str] = {}
    for entry in all_entries:
        eid = entry.get("id")
        if not eid or eid in seen_for_timeline:
            continue
        if entry.get("accepted") and entry.get("supersedes"):
            # Acceptance follow-up shares the id; skip so we keep the original
            # verdict event as the canonical timeline row.
            continue
        seen_for_timeline.add(eid)
        prompt = entry.get("prompt") or ""
        if not prompt:
            continue
        verdict = entry.get("verdict") or {}
        rating = (verdict.get("rating") or "").lower() or "unknown"
        timeline_by_prompt.setdefault(prompt, []).append(
            {
                "rating": rating,
                "judge_model": entry.get("judge_model"),
                "created_at": entry.get("created_at"),
                "reason_snippet": _reason_snippet(verdict.get("reason")),
            }
        )
        if prompt not in signature_by_prompt:
            sig = entry.get("prompt_signature") or _sig(prompt)
            signature_by_prompt[prompt] = sig

    # Sort timelines oldest-first by created_at (string ISO compare is fine).
    for rows in timeline_by_prompt.values():
        rows.sort(key=lambda r: r.get("created_at") or "")

    most_corrected = []
    for p, c in corrected_counts.most_common(10):
        most_corrected.append(
            {
                "prompt": p,
                "count": c,
                "prompt_signature": signature_by_prompt.get(p) or _sig(p),
                "verdict_timeline": timeline_by_prompt.get(p, []),
            }
        )

    return {
        "total": total,
        "pass_rate": _rate(passes, total),
        "total_7d": total_7d,
        "pass_rate_7d": _rate(passes_7d, total_7d),
        "rating_counts": dict(rating_counts),
        "trend_7d": trend,
        "most_corrected_prompts": most_corrected,
        "latency_histogram": _latency_histogram(by_id.values()),
        "log_path": str(log_path()),
    }
