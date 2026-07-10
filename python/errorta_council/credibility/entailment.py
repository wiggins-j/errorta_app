"""F081 entailment gate (pure seam, fail-closed).

Decides whether a *fetched source* actually supports a claim BEFORE the claim
can be admitted — moving "does this source support the claim?" from prompt
theater into a code-enforced admission input. No HTTP, no gateway import: the
model call is an injected async callable so this module stays a pure seam
(invariant 3). Source bytes stay inside the call; only the supporting span +
its hash leave it (invariant 5).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

# The graded vocabulary F081 introduces (distinct from REVIEW_STATUSES /
# SUPPORT_QUALITIES). "unresolved" is the fail-closed sentinel — the verifier
# could not decide, so the claim is held, never admitted.
ENTAILMENT_GRADES: tuple[str, ...] = (
    "entails", "overclaim", "inference", "partially_entails",
    "unsupported", "contradicts",
)
UNRESOLVED = "unresolved"

NEUTRAL_ENTAILMENT_PROMPT = (
    "You are a neutral citation verifier. You have NO opinion on the topic. "
    "Given a CLAIM and excerpts from ONE fetched SOURCE, decide ONLY how the "
    "source's text relates to the claim. Do not use outside knowledge. The key "
    "distinction: does the source say LESS than the claim (it supports a weaker "
    "version) or does it say NOTHING about the claim (the claim is an inference "
    "beyond the source)? Respond with a SINGLE JSON object and nothing else:\n"
    '{"grade": "entails|overclaim|inference|unsupported|contradicts", "span": '
    '"<the verbatim sentence from the source that best supports or refutes the '
    'claim, or empty>", "revised_text": "<ONLY when grade is overclaim: the '
    'strongest claim the source actually supports, in one sentence; else empty>", '
    '"reason": "<one short sentence>"}\n'
    "Use 'entails' if the source clearly states or directly implies the claim; "
    "'overclaim' if the source supports a WEAKER version (give revised_text); "
    "'inference' if the source is SILENT on the claim (it's a leap beyond the "
    "source); 'unsupported' if the source is on-topic but doesn't support it; "
    "'contradicts' if the source argues the opposite."
)

# call_model(system_prompt, user_prompt) -> model text
CallModel = Callable[[str, str], Awaitable[str]]

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "the a an of to in is are and or for on at by with as be this that it its "
    "from was were not but they their he she his her you your we our".split()
)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _terms(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOP and len(w) > 2}


def select_candidate_spans(
    source_text: str, claim_text: str, *, k: int = 3, window: int = 600
) -> list[str]:
    """Lexical top-K windows of the source most relevant to the claim, so the
    verifier never receives a whole page. Falls back to the head of the source
    when nothing overlaps."""
    text = (source_text or "").strip()
    if not text:
        return []
    claim_terms = _terms(claim_text)
    if not claim_terms:
        return [text[:window]]
    # Slide non-overlapping windows; score by claim-term overlap.
    windows: list[tuple[int, str]] = []
    for i in range(0, len(text), window):
        chunk = text[i : i + window]
        score = len(_terms(chunk) & claim_terms)
        if score:
            windows.append((score, chunk))
    if not windows:
        return [text[:window]]
    windows.sort(key=lambda t: t[0], reverse=True)
    return [c for _s, c in windows[:k]]


@dataclass(frozen=True)
class EntailmentResult:
    grade: str = UNRESOLVED
    span: str = ""
    span_sha256: str = ""
    source_sha256: str = ""
    reason: str = ""
    revised_text: str = ""  # F082: the weaker claim the source supports (overclaim)
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def supports(self) -> bool:
        # An overclaim DOES support a (weaker) version; an inference does not.
        return self.grade in ("entails", "overclaim", "partially_entails")

    @property
    def is_caveat(self) -> bool:
        return self.grade in ("overclaim", "partially_entails")


def _parse(raw: str, source_sha256: str) -> EntailmentResult:
    obj = _extract_json_object(raw)
    if not isinstance(obj, dict):
        return EntailmentResult(grade=UNRESOLVED, source_sha256=source_sha256)
    grade = str(obj.get("grade") or obj.get("judgment") or "").strip().lower()
    synonyms = {
        "entailed": "entails", "supported": "entails", "verified": "entails",
        "partial": "partially_entails", "partially_supported": "partially_entails",
        "indirect": "partially_entails", "not_supported": "unsupported",
        "no_support": "unsupported", "contradicted": "contradicts",
        "refutes": "contradicts",
        # F082 new grades + synonyms
        "overclaimed": "overclaim", "weaker": "overclaim",
        "inferred": "inference", "silent": "inference", "not_addressed": "inference",
    }
    grade = synonyms.get(grade, grade)
    if grade not in ENTAILMENT_GRADES:
        return EntailmentResult(grade=UNRESOLVED, source_sha256=source_sha256)
    span = str(obj.get("span") or "").strip()[:500]
    revised = str(obj.get("revised_text") or "").strip()[:400] if grade == "overclaim" else ""
    return EntailmentResult(
        grade=grade, span=span, span_sha256=_sha(span) if span else "",
        source_sha256=source_sha256, reason=str(obj.get("reason") or "").strip()[:200],
        revised_text=revised,
    )


def aggregate_grades(grades: list[str]) -> str:
    """Multi-source precedence (F081 + F082): a contradicting source poisons the
    claim; else an exact entailment wins; else an overclaim (a weaker version is
    supported) over a bare inference; else the best available. Every grade in
    ENTAILMENT_GRADES MUST appear here or it silently degrades to UNRESOLVED."""
    g = set(grades)
    for grade in ("contradicts", "entails", "overclaim", "partially_entails",
                  "inference", "unsupported"):
        if grade in g:
            return grade
    return UNRESOLVED


_HOST_RE = re.compile(r"https?://([^/\s:]+)")
_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def _host_of(url: str) -> str:
    m = _HOST_RE.match(url.strip())
    return m.group(1).lower().removeprefix("www.") if m else ""


def extract_prose_citations(
    text: str, source_urls: list[str]
) -> list[tuple[str, str]]:
    """F082 Pillar 2: pull factual assertions stated as FREE PROSE that reference
    a fetched source (by host or URL) — the citations that never emit a
    structured claim packet and so escape the gate today. Returns
    [(sentence, source_url)]."""
    if not text or not source_urls:
        return []
    hosts: dict[str, str] = {}
    for u in source_urls:
        h = _host_of(u)
        if h:
            hosts.setdefault(h, u)
    out: list[tuple[str, str]] = []
    for sent in _SENT_RE.split(text):
        s = sent.strip()
        if len(s.split()) < 5:
            continue
        low = s.lower()
        for h, u in hosts.items():
            if h in low or u in s:
                out.append((s, u))
                break
    return out


class EntailmentVerifier(Protocol):
    async def verify(
        self, *, claim_text: str, source_text: str, source_sha256: str
    ) -> EntailmentResult: ...


class GatewayEntailmentVerifier:
    """Runs the entailment check via an injected model call. Memoizes by
    (claim_text_sha256, source_content_sha256) so re-checks are free. Fail-closed:
    any error → ``unresolved`` (the admission gate then holds the claim)."""

    def __init__(
        self, call_model: CallModel, *, cache: dict | None = None, k: int = 3
    ) -> None:
        self._call = call_model
        self._cache: dict[tuple[str, str], EntailmentResult] = (
            cache if cache is not None else {}
        )
        self._k = k

    async def verify(
        self, *, claim_text: str, source_text: str, source_sha256: str
    ) -> EntailmentResult:
        ssha = source_sha256 or _sha(source_text or "")
        key = (_sha(claim_text or ""), ssha)
        if key in self._cache:
            return self._cache[key]
        candidates = select_candidate_spans(source_text, claim_text, k=self._k)
        excerpts = "\n---\n".join(candidates) if candidates else (source_text or "")[:1800]
        user = (
            f"CLAIM:\n{claim_text}\n\nSOURCE EXCERPTS (from the fetched page):\n"
            f"{excerpts}\n\nDoes the source support the claim?"
        )
        try:
            raw = await self._call(NEUTRAL_ENTAILMENT_PROMPT, user)
            result = _parse(str(raw), ssha)
        except Exception:
            result = EntailmentResult(grade=UNRESOLVED, source_sha256=ssha)
        self._cache[key] = result
        return result


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
