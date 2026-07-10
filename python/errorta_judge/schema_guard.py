"""Schema guard for the LLM-judge Verdict object.

The judge model is asked for JSON with a known shape; in practice the
output drifts (extra prose, code fences, partial JSON, alternate keys).
``normalize_verdict`` returns a clean dict that the rest of the system
can rely on, falling back to ``failure_tags=['judge_unparseable']`` after
two retries' worth of normalization attempts have failed.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

VALID_RATINGS = ("pass", "partial", "fail")
MAX_REPAIR_ATTEMPTS = 2

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)
_FIRST_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _strip_fences(s: str) -> str:
    return _FENCE_RE.sub("", s).strip()


def _try_parse(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    candidates: list[str] = [raw, _strip_fences(raw)]
    m = _FIRST_JSON_RE.search(raw)
    if m:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            continue
    return None


def _coerce_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # comma-separated fallback
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, Iterable):
        return [str(t).strip() for t in value if str(t).strip()]
    return []


def _coerce_confidence(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return max(0.0, min(1.0, f))


def _coerce_rating(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in VALID_RATINGS:
        return v
    # common aliases
    if v in ("partial-pass", "partially correct", "mixed"):
        return "partial"
    if v in ("correct", "ok", "good"):
        return "pass"
    if v in ("incorrect", "wrong", "bad"):
        return "fail"
    return None


def normalize_verdict(raw: Any) -> dict[str, Any]:
    """Best-effort normalize whatever the judge returned into our shape.

    Accepts either a dict (already parsed) or a string (raw model output).
    Always returns a dict with at least ``rating`` and ``failure_tags``.
    On total failure returns ``{rating: 'fail', failure_tags: ['judge_unparseable']}``.
    """
    obj: dict[str, Any] | None = None
    if isinstance(raw, dict):
        obj = raw
    elif isinstance(raw, str):
        for _ in range(MAX_REPAIR_ATTEMPTS + 1):
            obj = _try_parse(raw)
            if obj is not None:
                break
            # Successive repair passes do progressively more aggressive cleanup.
            raw = _strip_fences(raw)
    if obj is None:
        return {
            "rating": "fail",
            "reason": "Judge output was not valid JSON.",
            "failure_tags": ["judge_unparseable"],
            "confidence": None,
        }

    rating = _coerce_rating(obj.get("rating") or obj.get("verdict") or obj.get("score"))
    tags = _coerce_tags(obj.get("failure_tags") or obj.get("tags"))
    confidence = _coerce_confidence(obj.get("confidence"))
    reason_val = obj.get("reason") or obj.get("rationale") or obj.get("explanation")
    reason = reason_val.strip() if isinstance(reason_val, str) else None

    if rating is None:
        return {
            "rating": "fail",
            "reason": reason or "Judge rating missing or unrecognized.",
            "failure_tags": list({*tags, "judge_unparseable"}),
            "confidence": confidence,
        }

    return {
        "rating": rating,
        "reason": reason,
        "failure_tags": tags,
        "confidence": confidence,
    }
