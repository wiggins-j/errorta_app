"""Token estimation and calibration helpers for Council context packing.

F036 WS1 replaces the old whitespace-only estimate used for admission
decisions. The heuristic intentionally avoids a tokenizer dependency; provider
reported usage can calibrate it per provider/model when available.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MIN_CALIBRATION_FACTOR = 0.7
MAX_CALIBRATION_FACTOR = 3.0
DEFAULT_EMA_ALPHA = 0.3

_KIND_CONSTANTS: dict[str, tuple[float, float]] = {
    "prose": (1.32, 4.0),
    "code": (1.9, 3.2),
    "json": (1.9, 3.0),
    "mixed": (1.55, 3.5),
}


class TokenEstimator(Protocol):
    @property
    def method(self) -> str: ...

    @property
    def calibration_factor(self) -> float: ...

    def estimate(self, text: str, *, content_kind: str = "prose") -> int: ...


class WhitespaceEstimator:
    """Compatibility estimator matching the pre-F036 behavior."""

    method = "whitespace"
    calibration_factor = 1.0

    def estimate(self, text: str, *, content_kind: str = "prose") -> int:
        del content_kind
        return max(1, len(str(text).split()))


class HeuristicEstimator:
    """Blended word/character heuristic with no external dependency."""

    method = "heuristic_v1"
    calibration_factor = 1.0

    def estimate(self, text: str, *, content_kind: str = "prose") -> int:
        text = str(text)
        if not text:
            return 1
        words = len(text.split())
        chars = len(text)
        word_k, char_k = _KIND_CONSTANTS.get(content_kind, _KIND_CONSTANTS["prose"])
        return max(1, int(round(max(words * word_k, chars / char_k))))


@dataclass(frozen=True)
class CalibrationSample:
    provider: str
    model: str
    ratio: float


class CalibratedEstimator:
    """Wrap a base estimator with a clamped provider/model factor."""

    method = "calibrated_heuristic_v1"

    def __init__(self, base: TokenEstimator | None = None, *, factor: float = 1.0) -> None:
        self._base = base or HeuristicEstimator()
        self._factor = clamp_factor(factor)

    @property
    def calibration_factor(self) -> float:
        return self._factor

    def estimate(self, text: str, *, content_kind: str = "prose") -> int:
        return max(1, int(math.ceil(self._base.estimate(text, content_kind=content_kind) * self._factor)))


def clamp_factor(value: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(f):
        return 1.0
    return min(MAX_CALIBRATION_FACTOR, max(MIN_CALIBRATION_FACTOR, f))


def update_ema(current: float | None, sample: float, *, alpha: float = DEFAULT_EMA_ALPHA) -> float:
    sample = clamp_factor(sample)
    if current is None:
        return sample
    return clamp_factor((alpha * sample) + ((1.0 - alpha) * clamp_factor(current)))


class TokenCalibrationStore:
    """Small JSON store keyed by provider/model.

    Shape:
      {"factors": {"provider/model": 1.08}}
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def read_factor(self, provider: str, model: str) -> float:
        return clamp_factor(self.read_all().get(_key(provider, model), 1.0))

    def read_all(self) -> dict[str, float]:
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        factors = raw.get("factors") if isinstance(raw, dict) else None
        if not isinstance(factors, dict):
            return {}
        out: dict[str, float] = {}
        for key, value in factors.items():
            if isinstance(key, str):
                out[key] = clamp_factor(value)
        return out

    def write_all(self, factors: dict[str, float]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        normalized = {str(k): clamp_factor(v) for k, v in sorted(factors.items())}
        payload = {"format": "errorta.token_calibration.v1", "factors": normalized}
        tmp = self._path.with_suffix(f"{self._path.suffix}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        os.replace(tmp, self._path)

    def record(self, sample: CalibrationSample, *, alpha: float = DEFAULT_EMA_ALPHA) -> float:
        factors = self.read_all()
        key = _key(sample.provider, sample.model)
        factors[key] = update_ema(factors.get(key), sample.ratio, alpha=alpha)
        self.write_all(factors)
        return factors[key]


def calibration_ratio(*, reported_input_tokens: int | None, estimated_input_tokens: int | None) -> float | None:
    if not reported_input_tokens or not estimated_input_tokens:
        return None
    if reported_input_tokens <= 0 or estimated_input_tokens <= 0:
        return None
    return clamp_factor(reported_input_tokens / estimated_input_tokens)


def content_kind_for_class(class_: str) -> str:
    if class_ in {"digest", "digest_v1", "metadata"}:
        return "json"
    if class_ in {"retrieved_snippet", "redacted_snippet", "source_excerpt"}:
        return "mixed"
    if class_ in {"code", "tool_output"}:
        return "code"
    # F143-01 coding-team composition taxonomy (spec §composition). These classes
    # carry code/diffs/retrieval, so they estimate as code/mixed rather than prose;
    # Council never emits them, so this is additive and leaves Council estimates
    # unchanged. Unlisted coding classes (role_instructions, work_request,
    # tool_guidance, transcript, prior_outputs) fall through to prose.
    if class_ in {"repo_snapshot", "pr_diff"}:
        return "code"
    if class_ == "project_context":
        return "mixed"
    return "prose"


def _key(provider: str, model: str) -> str:
    return f"{provider or 'unknown'}/{model or 'unknown'}"


__all__ = [
    "CalibrationSample",
    "CalibratedEstimator",
    "HeuristicEstimator",
    "TokenCalibrationStore",
    "TokenEstimator",
    "WhitespaceEstimator",
    "calibration_ratio",
    "clamp_factor",
    "content_kind_for_class",
    "update_ema",
]
