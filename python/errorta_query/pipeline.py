"""The AIAR boundary for the query pipeline (F001).

The single seam to AIAR's answerer/judge/grounding is expressed as a
``typing.Protocol`` (``Pipeline``) the routes depend on. The real AIAR adapter
(``errorta_judge.aiar_adapter.AiarPipeline``, which *does* ``import aiar`` and
calls ``pipeline.answer_prompt(judge=True)`` + ``aiar.grounding.store``)
satisfies the same protocol. ``StubPipeline`` ships in this module so the full
query -> judge -> accept -> reground loop runs with no AIAR and no Ollama.
**No ``import aiar`` anywhere here.**
"""

from __future__ import annotations

from typing import Optional, Protocol

from . import grounding
from .models import AnswerResult, QueryResult, Retrieval, Verdict
from .signature import prompt_signature


class Pipeline(Protocol):
    def answer(
        self,
        *,
        prompt: str,
        corpus: str,
        judge: bool,
        reground: bool,
        model: Optional[str],
    ) -> AnswerResult: ...

    def query(
        self,
        *,
        prompt: str,
        corpus_ids: list[str],
        top_k: int,
    ) -> list[QueryResult]: ...


class StubPipeline:
    """Deterministic answerer with no AIAR and no Ollama (dev seam backing).

    Produces a canned-but-sensible answer that quotes the prompt and names the
    corpus, computes the prompt signature, and — when ``reground`` is on and a
    correction exists for that signature in the local grounding store — prepends
    a note acknowledging the prior correction and sets
    ``retrieval.reground_applied``. With ``judge`` on it returns a deterministic
    usable ``Verdict``; with it off the verdict is None. ``aiar`` is always
    False (this is the stub).
    """

    def answer(self, *, prompt, corpus, judge, reground, model=None) -> AnswerResult:
        signature = prompt_signature(prompt)

        reground_applied = False
        prefix = ""
        if reground:
            correction = grounding.lookup(signature)
            if correction:
                reground_applied = True
                prefix = f"Considering your earlier correction: {correction}\n\n"

        prompt_clean = (prompt or "").strip()
        answer = (
            f"{prefix}Based on the '{corpus}' corpus, here is a response to "
            f'your question: "{prompt_clean}". (This is a deterministic '
            "development answer from the Errorta stub pipeline; the real "
            "answer comes from AIAR once wired in.)"
        )

        verdict: Optional[Verdict] = None
        if judge:
            verdict = Verdict(
                rating="good",
                reason=(
                    "The answer addresses the prompt and stays grounded in the "
                    f"'{corpus}' corpus without unsupported claims."
                ),
                failure_tags=[],
                confidence=0.9,
                usable=True,
            )

        retrieval = Retrieval(
            grounded=True,
            reground_applied=reground_applied,
            top_k=4,
            chunks_used=4,
        )

        return AnswerResult(
            answer=answer,
            model=model,
            verdict=verdict,
            retrieval=retrieval,
            prompt_signature=signature,
            aiar=False,
        )

    def query(
        self,
        *,
        prompt: str,
        corpus_ids: list[str],
        top_k: int,
    ) -> list[QueryResult]:
        """F031-RETRIEVAL: stub returns []. No AIAR, no retrieval.

        Council's RetrievalSeam already short-circuits on empty corpus_ids,
        but we also return [] here so a pinned-stub default_pipeline() in
        tests produces the documented no-AIAR behavior.
        """
        return []


class UnavailablePipeline:
    """Fail-closed pipeline for an explicitly selected but unusable AIAR runtime."""

    def __init__(self, reason: str, *, tag: str = "aiar_unavailable") -> None:
        self.reason = reason
        self.tag = tag

    def answer(self, *, prompt, corpus, judge, reground, model=None) -> AnswerResult:
        signature = prompt_signature(prompt)
        return AnswerResult(
            answer="",
            model=model,
            verdict=Verdict(
                rating="fail",
                reason=self.reason,
                failure_tags=[self.tag],
                confidence=None,
                usable=False,
            ),
            retrieval=Retrieval(
                grounded=False,
                reground_applied=False,
                top_k=0,
                chunks_used=0,
            ),
            prompt_signature=signature,
            aiar=False,
        )

    def query(
        self,
        *,
        prompt: str,
        corpus_ids: list[str],
        top_k: int,
    ) -> list[QueryResult]:
        return []


class _RemoteRetrievalPipeline:
    """F096 B1: wrap a local pipeline so retrieval (``query``) is served by the
    configured remote AIAR (example-host), while answer/grounding stay on ``inner``.

    This closes the F096 coordination gap: corpora are configured/listed via the
    project-grounding remote AIAR, but Council/judge retrieval used to key off
    residency only. When a remote AIAR is configured, Council retrieval now pulls
    real ``aiar.retrieve.v1`` chunks from it regardless of residency mode. The
    judge/answer data plane is untouched (delegated verbatim to ``inner``).
    """

    def __init__(self, inner: "Pipeline") -> None:
        self._inner = inner

    def answer(self, **kwargs) -> AnswerResult:
        return self._inner.answer(**kwargs)

    def record_grounding(self, **kwargs):
        return self._inner.record_grounding(**kwargs)

    def query(self, *, prompt: str, corpus_ids: list[str],
              top_k: int) -> list[QueryResult]:
        from .aiar_retrieve import remote_aiar_retrieve

        return remote_aiar_retrieve(prompt=prompt, corpus_ids=corpus_ids, top_k=top_k)

    def query_strict(self, *, prompt: str, corpus_ids: list[str],
                     top_k: int) -> list[QueryResult]:
        """Pure-retrieve for SDK Service API callers: fail instead of degrading.

        F009-02: without this, the Service API's strict path silently fell back
        to best-effort ``query`` for the (real, production) remote-AIAR retrieval
        config, so a retrieval-backend outage produced a 200 ``no_hits`` answer
        instead of failing closed."""
        from .aiar_retrieve import remote_aiar_retrieve

        return remote_aiar_retrieve(
            prompt=prompt, corpus_ids=corpus_ids, top_k=top_k, strict=True)


def _maybe_wrap_remote_retrieval(inner: "Pipeline") -> "Pipeline":
    """Wrap ``inner`` to serve retrieval from a configured remote AIAR; otherwise
    return ``inner`` unchanged. Never raises — a resolver failure leaves the local
    pipeline in place."""
    try:
        from .backend import aiar_retrieval_target

        if aiar_retrieval_target() is not None:
            return _RemoteRetrievalPipeline(inner)
    except Exception:  # pragma: no cover - defensive
        pass
    return inner


def _local_answer_pipeline() -> "Pipeline":
    """Local answer/judge path (F001-SEAM): prefer ``AiarPipeline`` when AIAR is
    importable, else ``StubPipeline``. Retrieval may still be served from a
    configured remote AIAR via ``_maybe_wrap_remote_retrieval`` (it overrides only
    ``query``). Never raises."""
    try:
        from errorta_judge.aiar_adapter import AiarPipeline  # noqa: WPS433
    except Exception:
        return _maybe_wrap_remote_retrieval(StubPipeline())
    try:
        return _maybe_wrap_remote_retrieval(AiarPipeline())
    except Exception:
        return _maybe_wrap_remote_retrieval(StubPipeline())


def default_pipeline() -> Pipeline:
    """Return the active pipeline.

    Residency dispatch (F-INFRA-12 Phase B Slice 8) runs first:

    * ``mode == "ssh-remote"`` with a configured ``local_tunnel_port``
      → ``RemoteHttpPipeline`` aimed at ``http://127.0.0.1:{port}`` (the
      local end of the SSH tunnel).
    * ``mode == "cloud"`` with a configured ``cloud_url`` →
      ``RemoteHttpPipeline`` aimed at that URL. The cloud token is
      held in-process only (never persisted) and is **not** visible to
      this factory; the v0.6 cloud-auth slice plumbs it through.
    * Any half-applied state (e.g. ``ssh-remote`` without a port) or
      a residency load failure falls through to the existing local
      path with a warning, so a misconfigured Settings save can't lock
      the user out of the judge surface entirely.

    Local path (unchanged from F001-SEAM): prefer
    ``AiarPipeline`` (``errorta_judge.aiar_adapter``) when AIAR is
    importable, else ``StubPipeline``. The adapter — not this module —
    owns the ``import aiar`` so this package stays AIAR-free.

    **No caching.** Residency state is re-read on every call, so a
    mode switch from the Settings panel becomes effective on the next
    judge request without a sidecar restart. If this ever shows up as
    a hot path we add a cache + invalidate-on-PUT in a later slice.
    """
    import logging as _logging

    _log = _logging.getLogger(__name__)

    # F116: the active AIAR connection authority wins. This handles raw AIAR
    # services (example-host) separately from a full remote Errorta sidecar.
    try:
        from errorta_aiar_connection import resolve_aiar_runtime  # noqa: WPS433

        runtime = resolve_aiar_runtime()
        if runtime.kind == "aiar-service" and runtime.base_url:
            caps = runtime.capabilities
            if runtime.connected and not (caps.answer or caps.judge):
                return UnavailablePipeline(
                    "The selected AIAR service does not advertise answer or judge capability.",
                    tag="aiar_capability_missing",
                )
            from .aiar_service_pipeline import AiarServicePipeline  # noqa: WPS433

            return AiarServicePipeline(
                runtime.base_url,
                token=runtime.token,
                timeout_s=runtime.timeout_s,
                verify=runtime.verify_tls,
            )
        if runtime.kind == "errorta-sidecar-remote" and runtime.base_url:
            from .remote_pipeline import RemoteHttpPipeline  # noqa: WPS433

            return RemoteHttpPipeline(runtime.base_url, token=runtime.token, timeout_s=60.0)
        if runtime.kind == "local-aiar":
            # The canonical local choice is authoritative: build the local
            # pipeline directly rather than falling through to a (possibly stale)
            # residency dispatch that could route to a remote sidecar.
            return _local_answer_pipeline()
        if runtime.kind == "disconnected":
            if runtime.config_source == "canonical":
                return UnavailablePipeline(
                    "AIAR is disconnected in Settings.",
                    tag="aiar_disconnected",
                )
            return _maybe_wrap_remote_retrieval(StubPipeline())
    except Exception as exc:  # pragma: no cover — preserve legacy fallback
        _log.warning("AIAR connection resolver failed (%s); falling through", exc)

    # Lazy import: avoids any chance of an import cycle with
    # ``errorta_residency`` (which depends on ``errorta_app.paths``).
    try:
        from errorta_residency import config as residency_config  # noqa: WPS433

        state = residency_config.load()
    except Exception as exc:  # pragma: no cover — defensive
        _log.warning(
            "residency config load failed (%s); falling through to local pipeline",
            exc,
        )
        state = None

    if state is not None:
        try:
            from .remote_pipeline import RemoteHttpPipeline  # noqa: WPS433

            if state.mode == "ssh-remote":
                port = state.local_tunnel_port
                if port:
                    base = f"http://127.0.0.1:{port}"
                    return RemoteHttpPipeline(base, timeout_s=60.0)
                _log.warning(
                    "residency mode=ssh-remote but local_tunnel_port is unset; "
                    "falling through to local pipeline"
                )
            elif state.mode == "cloud":
                _log.warning(
                    "residency mode=cloud is not enabled until token auth ships; "
                    "falling through to local pipeline"
                )
        except Exception as exc:  # pragma: no cover — defensive
            _log.warning(
                "remote pipeline construction failed (%s); falling through to local",
                exc,
            )

    # Local answer/judge path (retrieval may still be wrapped to a configured
    # remote AIAR — see _local_answer_pipeline / _maybe_wrap_remote_retrieval).
    return _local_answer_pipeline()
