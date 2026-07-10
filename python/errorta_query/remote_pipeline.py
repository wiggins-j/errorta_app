"""F-INFRA-12 Phase B Slice 8 — remote-sidecar Pipeline adapter.

``RemoteHttpPipeline`` satisfies the ``Pipeline`` protocol the judge router
consumes (see ``errorta_query.pipeline``) by **proxying** each call to a
remote sidecar's ``/judge/*`` HTTP surface. It is the residency-aware
seam: in ``ssh-remote`` mode the local sidecar stays up, runs the React
shell's local-only routes (``/residency``, ``/healthz``), and forwards
judge/corpus/briefs traffic over the SSH tunnel to the remote box's
sidecar (the one that actually has AIAR + Ollama + the corpus on disk).

The audit recommendation (spec §7 open question #1) is **proxy**, not
dormant-local-sidecar: single ``/healthz`` contract for the frontend,
``/residency`` and diagnostics stay local-only, and the residency state
is consulted on every ``default_pipeline()`` call — no caching, so a
mode switch becomes effective on the next judge request without a
restart.

**Security:** the cloud token (when present) is sent as
``X-Errorta-Token`` and is never logged, never echoed back in
``PipelineError`` messages.

**Fail-loud:** network errors and 5xx responses raise ``PipelineError``.
Silent fallback to the local AIAR/Stub path is rejected on purpose —
the local and remote corpora/state diverge, and masking a misconfigured
tunnel by silently answering off the laptop's empty corpus would
produce convincing-but-wrong verdicts.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .models import AnswerResult, QueryResult, Retrieval, Verdict
from .signature import prompt_signature

log = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    """Raised when a remote pipeline call cannot be completed.

    The string form is safe to log — it carries the network/HTTP reason
    but **never** the auth token. Tests assert this directly.
    """


class RemoteHttpPipeline:
    """Pipeline adapter that forwards judge calls to a remote sidecar.

    Implements the same surface as the AIAR-backed pipeline
    (``answer`` + ``record_grounding``) by issuing HTTP calls to
    ``{base_url}/judge/...`` and returning the parsed JSON. Used when
    ``ResidencyState.mode`` is ``ssh-remote`` (``base_url`` is the
    local SSH-tunnel port) or ``cloud`` (``base_url`` is the
    user-supplied HTTPS URL, with ``cloud_token`` sent as
    ``X-Errorta-Token``).
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: Optional[str] = None,
        timeout_s: float = 60.0,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = float(timeout_s)

    # ---------- helpers ----------

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Errorta-Token"] = self.token
        return headers

    def _redact(self, exc: BaseException) -> str:
        """Stringify an exception without leaking the auth token.

        ``httpx`` rarely embeds request headers in error messages, but
        the security regression test (``test_remote_pipeline.py``)
        validates this defensively.
        """
        msg = str(exc)
        if self.token and self.token in msg:
            msg = msg.replace(self.token, "<redacted>")
        return msg

    # ---------- query path ----------

    def answer(
        self,
        *,
        prompt: str,
        corpus: str = "",
        judge: bool = True,
        reground: bool = True,
        model: Optional[str] = None,
    ) -> AnswerResult:
        """Forward a prompt to the remote ``/judge/verdict`` endpoint.

        Builds the request body in the shape ``VerdictRequest`` expects
        (see ``errorta_app/routes/judge.py``), parses the response, and
        returns an ``AnswerResult`` shaped like the local-mode adapter
        so the route layer stays agnostic.

        Raises ``PipelineError`` on network error or 5xx response. We
        do **not** silently fall back to local execution — corpora and
        grounding state differ between local and remote, and answering
        off the laptop's empty corpus would yield convincing-but-wrong
        verdicts.
        """
        target = f"{self.base_url}/judge/verdict"
        body: dict[str, Any] = {"prompt": prompt}
        if corpus:
            body["corpus"] = corpus
        if model:
            body["judge_model"] = model

        try:
            with httpx.Client(timeout=httpx.Timeout(self.timeout_s)) as client:
                response = client.post(target, json=body, headers=self._headers())
        except (httpx.HTTPError, OSError) as exc:
            raise PipelineError(
                f"remote unreachable: {self._redact(exc)}"
            ) from None

        status = getattr(response, "status_code", None)
        if isinstance(status, int) and 500 <= status < 600:
            raise PipelineError(
                f"remote unreachable: upstream returned {status}"
            )
        if not (isinstance(status, int) and 200 <= status < 300):
            raise PipelineError(
                f"remote unreachable: upstream returned {status}"
            )

        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise PipelineError(
                f"remote unreachable: malformed JSON ({self._redact(exc)})"
            ) from None

        if not isinstance(payload, dict):
            raise PipelineError("remote unreachable: response was not an object")

        answer_text = payload.get("answer") or ""
        verdict_raw = payload.get("verdict")
        grounded = _optional_bool(payload.get("grounded"))
        reground_applied = _optional_bool(payload.get("reground_applied"))
        rag_enabled = _optional_bool(payload.get("rag_enabled"))
        latency = _optional_float(payload.get("latency"))

        verdict_obj: Optional[Verdict] = None
        if isinstance(verdict_raw, dict):
            failure_tags = verdict_raw.get("failure_tags") or []
            verdict_obj = Verdict(
                rating=verdict_raw.get("rating") or "unknown",
                reason=verdict_raw.get("reason") or "",
                failure_tags=list(failure_tags),
                confidence=verdict_raw.get("confidence"),
                usable="judge_unparseable" not in failure_tags,
            )

        retrieval = Retrieval(
            grounded=grounded if grounded is not None else True,
            reground_applied=reground_applied if reground_applied is not None else False,
            top_k=0,
            chunks_used=0,
        )
        sig = payload.get("prompt_signature") or prompt_signature(prompt)

        result = AnswerResult(
            answer=answer_text,
            model=payload.get("model") or payload.get("judge_model") or model,
            verdict=verdict_obj,
            retrieval=retrieval,
            prompt_signature=sig,
            aiar=True,  # the *remote* sidecar is AIAR-backed
            call_id=_optional_str(payload.get("call_id")),
            instance=_optional_str(payload.get("instance")) or (corpus or None),
            grounded=grounded,
            reground_applied=reground_applied,
            rag_enabled=rag_enabled,
            latency=latency,
        )
        # Mirror the AiarPipeline side-channel so the route layer's
        # schema_guard re-normalization still works against a proxied
        # response (it will be an already-normalized dict, which is a
        # fixed point of normalize_verdict).
        if isinstance(verdict_raw, dict):
            result.raw_verdict = verdict_raw  # type: ignore[attr-defined]
        return result

    # ---------- retrieval path ----------

    def query(
        self,
        *,
        prompt: str,
        corpus_ids: list[str],
        top_k: int,
    ) -> list[QueryResult]:
        """F096 B1: pure-retrieve from the configured remote AIAR (example-host).

        Delegates to ``aiar_retrieve.remote_aiar_retrieve``, which resolves the
        backend (+ token) via the B4 seam and queries AIAR's ``aiar.retrieve.v1``
        route. Returns ``[]`` when no remote AIAR is configured (the active
        residency sidecar exposes no pure-retrieve route yet — that stays a
        separate slice), so the Pipeline Protocol is still satisfied.
        """
        from .aiar_retrieve import remote_aiar_retrieve

        results = remote_aiar_retrieve(
            prompt=prompt, corpus_ids=corpus_ids, top_k=top_k)
        if not results and corpus_ids:
            logging.getLogger("errorta_query.remote_pipeline").warning(
                "no remote-AIAR retrieval target / no hits for %d corpus_ids",
                len(corpus_ids),
            )
        return results

    def query_strict(
        self,
        *,
        prompt: str,
        corpus_ids: list[str],
        top_k: int,
    ) -> list[QueryResult]:
        """Pure-retrieve for SDK Service API callers: fail instead of degrading."""
        from .aiar_retrieve import remote_aiar_retrieve

        return remote_aiar_retrieve(
            prompt=prompt,
            corpus_ids=corpus_ids,
            top_k=top_k,
            strict=True,
        )

    # ---------- grounding path ----------

    def record_grounding(
        self,
        *,
        prompt: str,
        answer: str,
        correction: Optional[str],
        verdict: Optional[dict[str, Any]],
        instance: Optional[str] = None,
    ) -> bool:
        """Forward an accept to the remote ``/judge/accept`` endpoint.

        Returns True on a 2xx response, False otherwise (including
        network errors). Grounding is best-effort from the caller's
        perspective — the route surfaces the boolean to the UI and the
        user can retry.

        Note on body shape: the remote ``/judge/accept`` route is
        ``id``-addressed (it looks up the verdict event in the remote
        sidecar's verdicts.jsonl). The local sidecar's route already
        owns the ``id`` flow; we forward the same body the route was
        given. Callers that have only ``prompt`` + ``answer`` + the
        verdict dict cannot drive ``/judge/accept`` directly — they
        are handled by the local stub grounding sink in routes/judge.py
        when this method returns False.

        Future v0.6 work: surface a ``/grounding/record`` route on the
        remote sidecar that's prompt-keyed (not event-id-keyed) so this
        adapter can forward the AIAR-style call directly. Today the
        prompt/answer/verdict triple is folded into a body the remote
        side will translate.
        """
        target = f"{self.base_url}/judge/accept"
        body: dict[str, Any] = {
            "prompt": prompt,
            "answer": answer,
        }
        if correction is not None:
            body["correction"] = correction
        if verdict is not None:
            body["verdict"] = verdict
        if instance is not None:
            body["instance"] = instance

        try:
            with httpx.Client(timeout=httpx.Timeout(self.timeout_s)) as client:
                response = client.post(target, json=body, headers=self._headers())
        except (httpx.HTTPError, OSError):
            return False

        status = getattr(response, "status_code", None)
        return isinstance(status, int) and 200 <= status < 300


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    return bool(value)


def _optional_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out
