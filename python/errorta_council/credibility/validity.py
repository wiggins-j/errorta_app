"""F082 argument-validity judge (pure seam, fail-closed).

When the entailment gate grades a claim ``inference`` — the cited source is
SILENT, so the claim is a leap beyond it — that's a reasoning question, not a
citation one. This judge assesses ONLY whether the leap is licensed by the
council's already-gated sourced facts. Like the entailment verifier it is a pure
seam over an injected ``call_model`` (no gateway/HTTP import, invariant 3); only
the verdict (judgment) leaves the call, never raw bytes (invariant 5).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

CallModel = Callable[[str, str], Awaitable[str]]

VALIDITY_VERDICTS: tuple[str, ...] = ("valid", "invalid")
UNRESOLVED = "unresolved"

NEUTRAL_VALIDITY_PROMPT = (
    "You are a neutral logic referee. You have NO opinion on the topic. A CLAIM "
    "below is an inference that goes BEYOND what any single source states. Given "
    "the claim and the SOURCED FACTS the council has already established, decide "
    "ONLY whether the claim is a VALID reasoning step from those facts (it "
    "follows, or is a reasonable inference) or INVALID (a non-sequitur or "
    "unsupported leap). Do NOT judge whether the claim is true in the world — "
    "only whether the established facts license it. Respond with a SINGLE JSON "
    'object and nothing else:\n{"verdict": "valid|invalid", "reason": "<one '
    'short sentence>"}'
)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ValidityResult:
    verdict: str = UNRESOLVED
    reason: str = ""
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)


_FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\s*\n?([\s\S]*?)\n?```$")


def _extract_json_object(content: str) -> dict[str, Any] | None:
    body = (content or "").strip()
    m = _FENCE_RE.match(body)
    if m:
        body = m.group(1).strip()
    try:
        obj = json.loads(body)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    start, end = body.find("{"), body.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(body[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def _parse(raw: str) -> ValidityResult:
    obj = _extract_json_object(raw)
    if not isinstance(obj, dict):
        return ValidityResult(UNRESOLVED)
    v = str(obj.get("verdict") or "").strip().lower()
    synonyms = {"sound": "valid", "follows": "valid", "licensed": "valid",
                "non_sequitur": "invalid", "unsupported": "invalid",
                "invalid_leap": "invalid"}
    v = synonyms.get(v, v)
    if v not in VALIDITY_VERDICTS:
        return ValidityResult(UNRESOLVED)
    return ValidityResult(verdict=v, reason=str(obj.get("reason") or "").strip()[:200])


class ArgumentValidityJudge:
    """Assess whether an inference is a valid step from the established facts.
    Fail-closed: any error → ``unresolved`` (the claim stays a flagged inference,
    never promoted to a sourced fact, never silently excluded)."""

    def __init__(self, call_model: CallModel, *, cache: dict | None = None) -> None:
        self._call = call_model
        self._cache: dict[tuple[str, str], ValidityResult] = (
            cache if cache is not None else {}
        )

    async def assess(
        self, *, claim_text: str, supporting_texts: list[str]
    ) -> ValidityResult:
        key = (_sha(claim_text or ""), _sha("|".join(sorted(supporting_texts))))
        if key in self._cache:
            return self._cache[key]
        facts = "\n- ".join(supporting_texts) if supporting_texts else "(none)"
        user = (
            f"CLAIM (an inference beyond any single source):\n{claim_text}\n\n"
            f"ESTABLISHED SOURCED FACTS:\n- {facts}\n\n"
            "Is the claim a valid reasoning step from these facts?"
        )
        try:
            result = _parse(str(await self._call(NEUTRAL_VALIDITY_PROMPT, user)))
        except Exception:
            result = ValidityResult(UNRESOLVED)
        self._cache[key] = result
        return result
