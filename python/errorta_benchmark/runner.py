"""Benchmark runner — POSTs prompts at /judge/verdict.

Failures yield a placeholder ``RecordedVerdict`` rather than raising. The
harness is meant to survive partial pipeline outages and produce a coherent
report from whatever did come back.

The HTTP transport is injectable via the ``client`` argument so tests can
pass a ``fastapi.testclient.TestClient`` instance. CI never reaches a real
Ollama daemon.

Live-run vs. mock mode is selected by the ``ERRORTA_REAL_BENCHMARK``
environment variable. When set to ``"1"``, ``_client_for_mode`` returns a
real ``httpx.Client``; otherwise it returns a deterministic mock client
whose verdicts depend only on the prompt text. Tests can still inject any
object satisfying the ``_PostClient`` protocol explicitly.

Three-phase REAL-mode flow (BENCH-WEDGE)
----------------------------------------
When ``re_run_paraphrase=True`` is requested in REAL mode AND grounding
embeddings are enabled (``ERRORTA_GROUNDING_EMBEDDINGS=1``), the runner
inserts a **wedge-amplification** phase between the primary pass and the
paraphrase re-run:

    primary pass  →  accept-correction on failed/partial primaries
                  →  paraphrase re-run

The accept phase POSTs a synthetic correction to ``/judge/accept`` for a
configurable fraction (default ~30%) of failed or partial primary verdicts. Each
accept call seeds the grounding store so the paraphrase re-run can light
up "similar" matches via embedding lookup — which is exactly what F024
measures. The phase is a strict no-op in FAKE mode, preserving existing
fake-mode behaviour.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Iterable, Optional, Protocol

import httpx

from .prompts import BenchmarkPrompt


class _PostClient(Protocol):
    """Minimal protocol satisfied by httpx.Client and TestClient."""

    def post(self, url: str, json: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class RecordedVerdict:
    """One verdict captured for a single prompt invocation."""

    prompt_id: str
    prompt_text: str
    is_paraphrase_re_run: bool
    rating: str  # "pass" | "partial" | "fail" | "uncertain" | "error"
    score: float  # 1.0 pass, 0.5 partial/uncertain, 0.0 fail/error
    answer: str = ""
    error: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)
    # BENCH-WEDGE: grounding_match passthrough from the verdict response.
    # Populated from response.json()["grounding_match"] when present.
    grounding_match_kind: Optional[str] = None  # "exact" | "similar" | None
    grounding_match_similarity: Optional[float] = None
    grounding_match_signature: Optional[str] = None
    # Server-side verdict id (used to drive the accept phase in REAL mode).
    verdict_id: Optional[str] = None


_RATING_SCORE = {
    "pass": 1.0,
    "partial": 0.5,
    "fail": 0.0,
    "uncertain": 0.5,
}


def _score_for(rating: str) -> float:
    return _RATING_SCORE.get(rating, 0.0)


# ---------------------------------------------------------------------------
# Deterministic mock client for scaffold-mode (no live judge).
# ---------------------------------------------------------------------------


# BENCH-REAL-FAKE phase names. The runner stamps one of these into each
# verdict payload via the ``_mock_phase`` sidecar field. Real routes ignore
# unknown fields via Pydantic's default extras policy; the mock client
# branches on it.
PHASE_PRIMARY = "primary"
PHASE_PARAPHRASE = "paraphrase"
PHASE_AFTER_PRIMARY = "after_correction_primary"
PHASE_AFTER_PARAPHRASE = "after_correction_paraphrase"

_VALID_PHASES = (
    PHASE_PRIMARY,
    PHASE_PARAPHRASE,
    PHASE_AFTER_PRIMARY,
    PHASE_AFTER_PARAPHRASE,
)


def _u_from_seed(prompt_id: str, phase: str) -> tuple[float, bytes]:
    """Stable uniform float in [0,1) plus the full digest for follow-on bits."""
    digest = sha256(f"{prompt_id}::{phase}".encode("utf-8")).digest()
    # First 4 bytes -> 32-bit unsigned int -> [0,1).
    u = int.from_bytes(digest[:4], "big") / (1 << 32)
    return u, digest


def _is_hallucination_wedge(prompt_id: str) -> bool:
    """Stable ~30% subset: prompt_id-hash mod 10 < 3."""
    h = sha256(prompt_id.encode("utf-8")).digest()
    return (h[0] % 10) < 3


def _confidence_for(rating: str, seed_byte: int) -> float:
    """Continuous confidence so median_score stops being a 4-bucket quantum."""
    b = seed_byte / 255.0
    if rating == "pass":
        return 0.6 + b * 0.35
    if rating == "uncertain":
        return 0.3 + b * 0.3
    # fail / error
    return 0.05 + b * 0.25


class _MockResponse:
    """Minimal stand-in for httpx.Response."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class DeterministicMockClient:
    """Reproducible, phase-aware mock client.

    The rating is fully determined by (prompt_id, phase) — same inputs in,
    same verdict out, across processes and runs. Falls back to a stable
    hash of the prompt text when neither sidecar field is present, so
    legacy callers (and the real route) keep their existing behaviour.

    Bucketing — see BENCH-REAL-FAKE for the rationale:

      * primary:    u<0.50 → pass, u<0.60 → uncertain, else fail. The
                    ~30% hallucination-wedge subset overrides to fail.
      * paraphrase: reuses the primary draw but degrades — primary
                    'uncertain' with u>0.5 flips to fail, and ~10% of
                    primary 'pass' flips to 'uncertain' (paraphrase-fragile).
                    Targets pass≈0.45, delta≈-0.05.
      * after_correction_primary / after_correction_paraphrase: same as
                    primary/paraphrase, except prompt ids in
                    ``corrected_ids`` flip to pass on both phases, and
                    ``similar_ids`` flip to pass on after_paraphrase with
                    a synthesized ``grounding_match.kind='similar'`` block.
    """

    def __init__(
        self,
        *,
        corrected_ids: Optional[Iterable[str]] = None,
        similar_ids: Optional[Iterable[str]] = None,
    ) -> None:
        self._corrected = set(corrected_ids or [])
        self._similar = set(similar_ids or [])

    def post(self, url: str, json: dict[str, Any]) -> _MockResponse:  # noqa: A002
        prompt_text = ""
        prompt_id = ""
        phase = PHASE_PRIMARY
        if isinstance(json, dict):
            prompt_text = str(json.get("prompt") or "")
            prompt_id = str(json.get("_mock_prompt_id") or "")
            ph = str(json.get("_mock_phase") or "")
            if ph in _VALID_PHASES:
                phase = ph

        # Legacy path: no prompt_id supplied. Stay deterministic on the
        # prompt-text hash so existing callers (and the wedge test, which
        # uses a real httpx MockTransport rather than this client) are
        # unaffected.
        if not prompt_id:
            return self._legacy_post(prompt_text)

        return self._phase_aware_post(prompt_id, prompt_text, phase)

    # ----- legacy fallback (no prompt_id) -----

    def _legacy_post(self, prompt_text: str) -> _MockResponse:
        digest = sha256(prompt_text.encode("utf-8")).digest()
        ratings = ("pass", "fail", "uncertain")
        rating = ratings[digest[0] % 3]
        body = {
            "_mock": True,
            "answer": f"[mock] {prompt_text[:60]}",
            "verdict": {
                "rating": rating,
                "reason": "deterministic mock verdict",
                "confidence": _confidence_for(rating, digest[1]),
            },
        }
        return _MockResponse(200, body)

    # ----- phase-aware path -----

    def _phase_aware_post(
        self, prompt_id: str, prompt_text: str, phase: str
    ) -> _MockResponse:
        is_wedge = _is_hallucination_wedge(prompt_id)
        # Compute the primary draw first; downstream phases reuse it.
        u_primary, primary_digest = _u_from_seed(prompt_id, PHASE_PRIMARY)
        rating = self._primary_rating(u_primary, is_wedge)

        # ~10% of primary 'pass' is paraphrase-fragile.
        para_fragile = (primary_digest[5] % 10) == 0

        grounding_match: Optional[dict[str, Any]] = None
        # Which digest byte feeds the continuous confidence.
        seed_byte = primary_digest[1]

        if phase == PHASE_PRIMARY:
            pass  # rating already set
        elif phase == PHASE_PARAPHRASE:
            rating = self._paraphrase_rating(
                rating, u_primary, para_fragile
            )
            _, para_digest = _u_from_seed(prompt_id, PHASE_PARAPHRASE)
            seed_byte = para_digest[1]
        elif phase == PHASE_AFTER_PRIMARY:
            _, after_digest = _u_from_seed(prompt_id, PHASE_AFTER_PRIMARY)
            seed_byte = after_digest[1]
            if prompt_id in self._corrected:
                rating = "pass"
            else:
                rating = self._primary_rating(u_primary, is_wedge)
        elif phase == PHASE_AFTER_PARAPHRASE:
            _, after_digest = _u_from_seed(prompt_id, PHASE_AFTER_PARAPHRASE)
            seed_byte = after_digest[1]
            if prompt_id in self._corrected:
                rating = "pass"
            elif prompt_id in self._similar:
                rating = "pass"
                # Synthesize a similar-grounding match.
                sim = 0.78 + (after_digest[2] / 255.0) * 0.14
                # Pick the corrected anchor deterministically.
                anchor = self._anchor_for_similar(prompt_id)
                grounding_match = {
                    "kind": "similar",
                    "similarity": sim,
                    "original_signature": anchor,
                }
            else:
                rating = self._paraphrase_rating(
                    self._primary_rating(u_primary, is_wedge),
                    u_primary,
                    para_fragile,
                )

        confidence = _confidence_for(rating, seed_byte)
        verdict = {
            "rating": rating,
            "reason": f"deterministic phase={phase}",
            "confidence": confidence,
        }
        body: dict[str, Any] = {
            "_mock": True,
            "answer": f"[mock:{phase}] {prompt_text[:60]}",
            "verdict": verdict,
        }
        if grounding_match is not None:
            body["grounding_match"] = grounding_match
        return _MockResponse(200, body)

    @staticmethod
    def _primary_rating(u: float, is_wedge: bool) -> str:
        if is_wedge:
            return "fail"
        # Non-wedge bucketing. The thresholds are tuned so that with the
        # welcome_v1 seed's ~30% always-failing wedge subset the overall
        # primary pass_rate lands in [0.45, 0.55] — the snapshot range
        # spelled out in the BENCH-REAL-FAKE spec. (Pure-wedge math:
        # 0.7 * 0.72 ≈ 0.504.)
        if u < 0.72:
            return "pass"
        if u < 0.82:
            return "uncertain"
        return "fail"

    @staticmethod
    def _paraphrase_rating(primary_rating: str, u: float, fragile: bool) -> str:
        # primary 'uncertain' with u>0.5 degrades to fail
        if primary_rating == "uncertain" and u > 0.5:
            return "fail"
        # ~10% of primary 'pass' flips to uncertain
        if primary_rating == "pass" and fragile:
            return "uncertain"
        return primary_rating

    def _anchor_for_similar(self, prompt_id: str) -> str:
        """Pick a deterministic corrected_id to cite as the original signature."""
        if not self._corrected:
            return prompt_id
        ordered = sorted(self._corrected)
        idx = sha256(prompt_id.encode("utf-8")).digest()[0] % len(ordered)
        return ordered[idx]


def _base_url_for_env() -> str:
    configured = os.environ.get("ERRORTA_JUDGE_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    sidecar_port = os.environ.get("ERRORTA_SIDECAR_PORT", "8770").strip() or "8770"
    return f"http://127.0.0.1:{sidecar_port}"


def _client_for_mode(
    base_url: Optional[str] = None,
    *,
    corrected_ids: Optional[Iterable[str]] = None,
    similar_ids: Optional[Iterable[str]] = None,
) -> _PostClient:
    """Pick a client based on the ``ERRORTA_REAL_BENCHMARK`` env var.

    - ``ERRORTA_REAL_BENCHMARK=1`` → real ``httpx.Client`` bound to ``base_url``
    - anything else                → :class:`DeterministicMockClient`
    """
    if os.environ.get("ERRORTA_REAL_BENCHMARK", "").lower() == "1":
        base_url = base_url or _base_url_for_env()
        return httpx.Client(base_url=base_url, timeout=180.0)
    return DeterministicMockClient(
        corrected_ids=corrected_ids, similar_ids=similar_ids
    )


def _is_real_mode() -> bool:
    return os.environ.get("ERRORTA_REAL_BENCHMARK", "").lower() == "1"


def _grounding_embeddings_enabled() -> bool:
    return os.environ.get("ERRORTA_GROUNDING_EMBEDDINGS", "").lower() == "1"


def _corpus_for_env() -> Optional[str]:
    corpus = os.environ.get("ERRORTA_BENCHMARK_CORPUS", "welcome").strip()
    return corpus or None


# Default fraction of failed/partial primaries to amplify via /judge/accept.
_DEFAULT_AMPLIFY_FRACTION = 0.30


def _amplify_subset(
    failed: list[RecordedVerdict], fraction: float
) -> list[RecordedVerdict]:
    """Pick a deterministic, ordered subset (~fraction) of failed verdicts.

    Selection is stable for a given input order so reports are reproducible.
    At least one entry is selected when ``failed`` is non-empty and fraction
    > 0.
    """
    if not failed or fraction <= 0:
        return []
    count = max(1, int(round(len(failed) * fraction)))
    return failed[:count]


class BenchmarkRunner:
    """Drive a sequence of prompts against /judge/verdict.

    Parameters
    ----------
    client:
        Any object exposing ``.post(url, json=...)`` returning a response with
        ``.status_code`` and ``.json()``. Tests pass a ``TestClient``. When
        ``None``, the runner consults :func:`_client_for_mode`.
    base_path:
        Mount path prefix. Defaults to "" (TestClient mounts the router with
        its own prefix already).
    """

    def __init__(
        self,
        client: Optional[_PostClient] = None,
        base_path: str = "",
        base_url: Optional[str] = None,
        corpus: Optional[str] = None,
    ) -> None:
        self._client = client if client is not None else _client_for_mode(base_url)
        self._base_path = base_path.rstrip("/")
        self._corpus = corpus if corpus is not None else _corpus_for_env()

    # ----- internal -----

    def _post_verdict(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        url = f"{self._base_path}/judge/verdict"
        resp = self._client.post(url, json=payload)
        status = getattr(resp, "status_code", 0)
        try:
            body = resp.json() if status else {}
        except Exception:
            body = {}
        return status, body or {}

    def _post_accept(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        url = f"{self._base_path}/judge/accept"
        resp = self._client.post(url, json=payload)
        status = getattr(resp, "status_code", 0)
        try:
            body = resp.json() if status else {}
        except Exception:
            body = {}
        return status, body or {}

    def _record_one(
        self,
        prompt: BenchmarkPrompt,
        is_paraphrase: bool,
        *,
        phase: Optional[str] = None,
    ) -> RecordedVerdict:
        text = prompt.paraphrase if is_paraphrase else prompt.text
        if phase is None:
            phase = PHASE_PARAPHRASE if is_paraphrase else PHASE_PRIMARY
        payload: dict[str, Any] = {
            "prompt": text,
            # Sidecar fields used by the fake-mode mock; real Pydantic routes
            # ignore unknown keys by default so they are inert in REAL mode.
            "_mock_prompt_id": prompt.id,
            "_mock_phase": phase,
        }
        if self._corpus:
            payload["corpus"] = self._corpus
        status, body = self._post_verdict(payload)

        if status != 200:
            return RecordedVerdict(
                prompt_id=prompt.id,
                prompt_text=text,
                is_paraphrase_re_run=is_paraphrase,
                rating="error",
                score=0.0,
                answer="",
                error=f"http {status}",
                raw=body,
            )

        verdict = body.get("verdict") or {}
        rating = str(verdict.get("rating") or "error")
        # BENCH-REAL-FAKE: when the body is tagged as coming from the
        # phase-aware fake mock (``_mock: true``), use the body's
        # continuous ``confidence`` field as the recorded ``score`` so
        # ``median_score`` stops collapsing onto the
        # {0, 0.25, 0.5, 0.75, 1.0} ladder. Real-route responses lack
        # the sentinel and keep the legacy bucketed score, so existing
        # tests are unaffected.
        is_mock_body = bool(body.get("_mock"))
        raw_conf = verdict.get("confidence") if isinstance(verdict, dict) else None
        try:
            conf_f: Optional[float] = (
                float(raw_conf) if raw_conf is not None else None
            )
        except (TypeError, ValueError):
            conf_f = None
        if is_mock_body and conf_f is not None and 0.0 <= conf_f <= 1.0:
            score = conf_f
        else:
            score = _score_for(rating)
        gm = body.get("grounding_match") or {}
        gm_kind = gm.get("kind") if isinstance(gm, dict) else None
        gm_sim = gm.get("similarity") if isinstance(gm, dict) else None
        gm_sig = gm.get("original_signature") if isinstance(gm, dict) else None
        try:
            gm_sim_f: Optional[float] = float(gm_sim) if gm_sim is not None else None
        except (TypeError, ValueError):
            gm_sim_f = None

        return RecordedVerdict(
            prompt_id=prompt.id,
            prompt_text=text,
            is_paraphrase_re_run=is_paraphrase,
            rating=rating,
            score=score,
            answer=str(body.get("answer") or ""),
            raw=body,
            grounding_match_kind=str(gm_kind) if gm_kind else None,
            grounding_match_similarity=gm_sim_f,
            grounding_match_signature=str(gm_sig) if gm_sig else None,
            verdict_id=str(body.get("id")) if body.get("id") else None,
        )

    def _amplify_failed_primaries(
        self, primaries: list[RecordedVerdict], fraction: float
    ) -> int:
        """REAL-mode wedge-amplification phase.

        POSTs a synthetic correction to ``/judge/accept`` for ~fraction of
        the failed or partial primary verdicts so the grounding store is seeded ahead
        of the paraphrase re-run. Returns the number of accept calls made.
        """
        failed = [
            v for v in primaries
            if v.rating in {"fail", "partial"} and v.verdict_id
        ]
        subset = _amplify_subset(failed, fraction)
        accepted = 0
        for v in subset:
            payload = {
                "id": v.verdict_id,
                "correction": (
                    f"[wedge-amplify] synthetic correction for prompt "
                    f"{v.prompt_id}: the verified answer differs from the "
                    "judge-flagged response. See seed-canonical guidance."
                ),
            }
            status, _body = self._post_accept(payload)
            if status == 200:
                accepted += 1
        return accepted

    # ----- public -----

    def orchestrate_run(
        self,
        prompts: Iterable[BenchmarkPrompt],
        re_run_paraphrase: bool = False,
        *,
        amplify_fraction: float = _DEFAULT_AMPLIFY_FRACTION,
    ) -> list[RecordedVerdict]:
        """Run each prompt, then (optionally) re-run with its paraphrase.

        In REAL mode with grounding embeddings enabled and paraphrase re-run
        requested, a wedge-amplification phase runs between the two passes;
        it is a no-op otherwise.
        """
        recorded: list[RecordedVerdict] = []
        prompt_list = list(prompts)
        primaries: list[RecordedVerdict] = []
        for p in prompt_list:
            v = self._record_one(p, is_paraphrase=False)
            recorded.append(v)
            primaries.append(v)

        # BENCH-WEDGE: amplification phase (REAL mode only).
        if (
            re_run_paraphrase
            and _is_real_mode()
            and _grounding_embeddings_enabled()
        ):
            self._amplify_failed_primaries(primaries, amplify_fraction)

        if re_run_paraphrase:
            for p in prompt_list:
                recorded.append(self._record_one(p, is_paraphrase=True))
        return recorded

    # ----- BENCH-REAL-FAKE: before/after simulator (fake mode only) -----

    def orchestrate_run_with_before_after(
        self,
        prompts: Iterable[BenchmarkPrompt],
        *,
        simulate_corrections: bool = True,
    ) -> tuple[list[RecordedVerdict], list[RecordedVerdict]]:
        """Run primary+paraphrase twice — once 'before', once 'after' simulated
        corrections — and return the two verdict lists. Designed for FAKE
        mode: the after pass swaps in a DeterministicMockClient whose
        ``corrected_ids`` are the first 10 hallucination-wedge prompts
        sorted by id, plus a small ``similar_ids`` neighborhood that flips
        on paraphrase with a synthesized grounding_match block.

        In REAL mode (or when ``simulate_corrections`` is False) the
        method still returns two passes but they will be identical modulo
        any real-world non-determinism — useful for shape-compat testing,
        not for the wedge story.
        """
        prompt_list = list(prompts)

        # BEFORE: phase=primary + phase=paraphrase, no corrections applied.
        before: list[RecordedVerdict] = []
        for p in prompt_list:
            before.append(self._record_one(p, is_paraphrase=False))
        for p in prompt_list:
            before.append(self._record_one(p, is_paraphrase=True))

        if not simulate_corrections:
            # Re-run identically; aggregator will see a zero delta.
            after: list[RecordedVerdict] = []
            for p in prompt_list:
                after.append(self._record_one(p, is_paraphrase=False))
            for p in prompt_list:
                after.append(self._record_one(p, is_paraphrase=True))
            return before, after

        # Pick corrected_ids: the first 10 hallucination-wedge prompts
        # sorted by id. Similar_ids: a small deterministic neighborhood
        # (each corrected_id picks one near-by prompt id whose hash byte
        # is within stable distance).
        wedges = sorted(p.id for p in prompt_list if _is_hallucination_wedge(p.id))
        corrected_ids = wedges[:10]
        similar_ids = self._similar_neighborhood(prompt_list, corrected_ids)

        # Swap the mock client for the after-correction pass. Only valid
        # in FAKE mode; in REAL mode we leave the real client in place
        # and these ids are inert (real route ignores _mock_phase).
        original_client = self._client
        try:
            if not _is_real_mode():
                self._client = DeterministicMockClient(
                    corrected_ids=corrected_ids,
                    similar_ids=similar_ids,
                )
            after = []
            for p in prompt_list:
                after.append(
                    self._record_one(
                        p, is_paraphrase=False, phase=PHASE_AFTER_PRIMARY
                    )
                )
            for p in prompt_list:
                after.append(
                    self._record_one(
                        p, is_paraphrase=True, phase=PHASE_AFTER_PARAPHRASE
                    )
                )
        finally:
            self._client = original_client

        return before, after

    @staticmethod
    def _similar_neighborhood(
        prompt_list: list[BenchmarkPrompt], corrected_ids: list[str]
    ) -> list[str]:
        """Stable lookup of 'similar' candidates.

        For each corrected_id, pick prompts whose first hash byte is
        within ±8 of the corrected_id's first hash byte. Excludes the
        corrected_ids themselves. Deterministic, small (a handful per
        run).
        """
        if not corrected_ids:
            return []
        corrected_set = set(corrected_ids)
        anchor_bytes = {
            cid: sha256(cid.encode("utf-8")).digest()[0]
            for cid in corrected_ids
        }
        out: set[str] = set()
        for p in prompt_list:
            if p.id in corrected_set:
                continue
            pb = sha256(p.id.encode("utf-8")).digest()[0]
            for ab in anchor_bytes.values():
                if abs(int(pb) - int(ab)) <= 8:
                    out.add(p.id)
                    break
        return sorted(out)


def orchestrate_run(
    prompts: Iterable[BenchmarkPrompt],
    *,
    client: Optional[_PostClient] = None,
    re_run_paraphrase: bool = False,
    base_path: str = "",
    base_url: Optional[str] = None,
    amplify_fraction: float = _DEFAULT_AMPLIFY_FRACTION,
    corpus: Optional[str] = None,
) -> list[RecordedVerdict]:
    """Module-level convenience wrapper.

    Constructs a :class:`BenchmarkRunner` with either the injected ``client``
    or the env-selected default from :func:`_client_for_mode`, then delegates
    to its :meth:`BenchmarkRunner.orchestrate_run`.
    """
    runner = BenchmarkRunner(
        client=client,
        base_path=base_path,
        base_url=base_url,
        corpus=corpus,
    )
    return runner.orchestrate_run(
        prompts,
        re_run_paraphrase=re_run_paraphrase,
        amplify_fraction=amplify_fraction,
    )
