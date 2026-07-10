"""AIAR adapter — the real ``Pipeline`` implementation (F001b).

``AiarPipeline`` satisfies ``errorta_query.pipeline.Pipeline`` and is the ONLY
module in the sidecar that is allowed to ``import aiar`` for the judge/query
path. It centralizes:

  * ``aiar.harness.pipeline.answer_prompt`` invocation, with the
    older-signature fallback that used to live inline in
    ``errorta_app.routes.judge._try_aiar_answer``;
  * defensive extraction of ``answer`` + raw judge verdict from AIAR's
    version-dependent return shape;
  * ``aiar.grounding.store.record`` invocation through AIAR's current
    signature/verdict/correction/instance contract.

The router calls ``adapter.answer(...)`` (or ``adapter.record_grounding(...)``)
and never touches ``aiar`` directly.
"""

from __future__ import annotations

from typing import Any, Optional

from errorta_judge import schema_guard
from errorta_query.models import AnswerResult, QueryResult, Retrieval, Verdict
from errorta_query.signature import prompt_signature


class AiarGroundingRecordError(RuntimeError):
    """Raised when AIAR exposes an incompatible grounding-record contract."""


class AiarPipeline:
    """Real AIAR-backed implementation of the ``Pipeline`` protocol.

    Construction imports AIAR eagerly so ``default_pipeline()`` can fail over
    to the stub when AIAR isn't installed in this environment.
    """

    def __init__(self) -> None:
        # Imported lazily-at-construction so the module is importable even
        # when AIAR isn't available (the default_pipeline factory will then
        # catch the ImportError and fall back to StubPipeline).
        from aiar.harness import pipeline as aiar_pipeline  # type: ignore

        self._aiar_pipeline = aiar_pipeline

    # ---------- query path ----------

    def _invoke_answer_prompt(
        self,
        prompt: str,
        corpus: Optional[str],
        judge: bool,
        judge_model: Optional[str],
    ) -> Any:
        """Call ``aiar.harness.pipeline.answer_prompt`` with the kwarg fallback.

        Older AIAR signatures reject some of the newer kwargs; on ``TypeError``
        we retry with the minimal positional + ``judge`` form.
        """
        # AIAR's answer_prompt uses `instance` (not `corpus`) to select the
        # RAG instance, and does NOT accept a `judge_model` kwarg (the judge
        # model is selected via the EVAL_JUDGE_MODEL env var). Earlier
        # versions of this adapter were guessing at AIAR's surface and
        # passing both `corpus=...` and `judge_model=...`, which raised
        # TypeError and fell back to a call WITHOUT instance — silently
        # losing corpus selection. Set EVAL_JUDGE_MODEL in the process env
        # before calling, then pass instance=corpus.
        import os
        prior_eval_model = os.environ.get("EVAL_JUDGE_MODEL")
        if judge_model:
            os.environ["EVAL_JUDGE_MODEL"] = judge_model
        kwargs: dict[str, Any] = {"judge": judge}
        if corpus:
            kwargs["instance"] = corpus
        try:
            return self._aiar_pipeline.answer_prompt(prompt, **kwargs)
        except TypeError:
            # Belt-and-braces: if a future AIAR rejects `instance`, fall back
            # to the active instance (which the operator can set via
            # aiar.rag.store.set_active before booting the sidecar).
            return self._aiar_pipeline.answer_prompt(prompt, judge=judge)
        finally:
            if judge_model:
                if prior_eval_model is None:
                    os.environ.pop("EVAL_JUDGE_MODEL", None)
                else:
                    os.environ["EVAL_JUDGE_MODEL"] = prior_eval_model

    def answer(
        self,
        *,
        prompt: str,
        corpus: str,
        judge: bool,
        reground: bool,
        model: Optional[str] = None,
    ) -> AnswerResult:
        """Run a prompt through AIAR and return a typed ``AnswerResult``.

        The raw judge output is attached as ``result.raw_verdict`` so the
        router can hand it to ``schema_guard.normalize_verdict`` — this keeps
        verdict-shape policy in one place (the route + schema_guard) while
        the adapter owns the AIAR call surface.
        """
        aiar_model = None
        call_id = None
        instance = corpus or None
        grounded = None
        reground_applied = None
        rag_enabled = None
        latency = None
        try:
            result = self._invoke_answer_prompt(prompt, corpus or None, judge, model)
        except Exception as exc:  # pragma: no cover - AIAR pipeline failure
            answer_text = ""
            raw_verdict: Any = {
                "rating": "fail",
                "reason": f"aiar pipeline error: {exc}",
                "failure_tags": ["aiar_pipeline_error"],
            }
        else:
            answer_text, raw_verdict = _extract_answer_and_verdict(result)
            aiar_model = _result_get(result, "model")
            call_id = _optional_str(_result_get(result, "call_id"))
            instance = _optional_str(_result_get(result, "instance")) or (corpus or None)
            grounded = _optional_bool(_result_get(result, "grounded"))
            reground_applied = _optional_bool(_result_get(result, "reground_applied"))
            rag_enabled = _optional_bool(_result_get(result, "rag_enabled"))
            latency = _optional_float(_result_get(result, "latency"))

        verdict_obj: Optional[Verdict] = None
        if judge:
            normalized = schema_guard.normalize_verdict(raw_verdict)
            verdict_obj = Verdict(
                rating=normalized.get("rating") or "fail",
                reason=normalized.get("reason") or "",
                failure_tags=list(normalized.get("failure_tags") or []),
                confidence=normalized.get("confidence"),
                usable="judge_unparseable" not in (normalized.get("failure_tags") or []),
            )

        retrieval = Retrieval(
            grounded=grounded if grounded is not None else True,
            reground_applied=reground_applied if reground_applied is not None else False,
            top_k=0,
            chunks_used=0,
        )
        ar = AnswerResult(
            answer=answer_text or "",
            model=_optional_str(aiar_model) or model,
            verdict=verdict_obj,
            retrieval=retrieval,
            prompt_signature=prompt_signature(prompt),
            aiar=True,
            call_id=call_id,
            instance=instance,
            grounded=grounded,
            reground_applied=reground_applied,
            rag_enabled=rag_enabled,
            latency=latency,
        )
        # Side-channel: the route hands this to schema_guard.normalize_verdict
        # so verdict-shape policy stays in one place.
        ar.raw_verdict = raw_verdict  # type: ignore[attr-defined]
        return ar

    # ---------- retrieval path (F031-RETRIEVAL) ----------

    def query(
        self,
        *,
        prompt: str,
        corpus_ids: list[str],
        top_k: int,
    ) -> list[QueryResult]:
        return self._query(prompt=prompt, corpus_ids=corpus_ids, top_k=top_k, strict=False)

    def query_strict(
        self,
        *,
        prompt: str,
        corpus_ids: list[str],
        top_k: int,
    ) -> list[QueryResult]:
        return self._query(prompt=prompt, corpus_ids=corpus_ids, top_k=top_k, strict=True)

    def _query(
        self,
        *,
        prompt: str,
        corpus_ids: list[str],
        top_k: int,
        strict: bool,
    ) -> list[QueryResult]:
        """F031-RETRIEVAL: retrieve top-k chunks per corpus_id, iterate-and-merge.

        PM decision (2026-06-12 #4): multi-corpus iterates; no fan-out +
        score-merge. Each corpus_id is queried independently; results
        concatenated in order then trimmed to top_k.

        PM decision (2026-06-12 #1): wraps ``answer_prompt(judge=False)``
        and extracts the retrieval block from the returned shape rather
        than touching unpushed AIAR ``dev`` to add a clean ``retrieve()``
        entry point. TODO(aiar-retrieve-clean-entry): once AIAR exposes a
        retrieval-only call, swap to it.

        Returns list[QueryResult]. Catches AIAR exceptions per corpus_id
        and continues; logs ``retrieval_adapter_query_failed`` at WARN
        level. Never raises into the adapter — Council policy is
        "retrieval failure does not block a turn" (spec §Failure modes).
        """
        import logging

        log = logging.getLogger(__name__)

        if not corpus_ids:
            return []

        results: list[QueryResult] = []
        for corpus_id in corpus_ids:
            try:
                raw_result = self._invoke_answer_prompt(
                    prompt, corpus_id, judge=False, judge_model=None,
                )
            except Exception as exc:
                log.warning(
                    "retrieval_adapter_query_failed corpus_id=%s class=%s",
                    corpus_id,
                    exc.__class__.__name__,
                )
                if strict:
                    raise
                continue

            chunks = _extract_retrieval_chunks(raw_result)
            if not chunks:
                continue
            emitted_before = len(results)

            # Shape-drift sentinel: a non-empty raw block where no chunk
            # exposes any of the canonical id fields is the canary for an
            # AIAR rev whose retrieval shape we no longer understand.
            if not any(
                _chunk_get(c, "id")
                or _chunk_get(c, "chunk_id")
                or _chunk_get(c, "citation_id")
                for c in chunks
            ):
                log.warning(
                    "retrieval_adapter_shape_drift corpus_id=%s n=%d",
                    corpus_id,
                    len(chunks),
                )
                if strict:
                    raise RuntimeError("retrieval_adapter_shape_drift")

            for chunk in chunks:
                content = _chunk_get(chunk, "text") or _chunk_get(chunk, "content") or ""
                if not content:
                    # QA P2: empty-content chunks (e.g., AIAR returns ids/scores
                    # but no text payload) must not surface as retrieved_snippet
                    # sources. Otherwise the inspection drawer reports populated
                    # source_counts while the gateway request actually carries
                    # no retrieval bytes. Treat as a shape-drift signal and skip.
                    log.warning(
                        "retrieval_adapter_empty_chunk_skipped corpus_id=%s chunk_id=%s",
                        corpus_id,
                        _chunk_get(chunk, "id") or _chunk_get(chunk, "chunk_id") or "",
                    )
                    continue
                chunk_id = (
                    _chunk_get(chunk, "id")
                    or _chunk_get(chunk, "chunk_id")
                    or ""
                )
                citation_id = _chunk_get(chunk, "citation_id") or ""
                score_raw = _chunk_get(chunk, "score")
                tokens_raw = _chunk_get(chunk, "tokens")
                try:
                    score = float(score_raw) if score_raw is not None else None
                except (TypeError, ValueError):
                    score = None
                try:
                    tokens = int(tokens_raw) if tokens_raw is not None else None
                except (TypeError, ValueError):
                    tokens = None
                results.append(
                    QueryResult(
                        content=str(content),
                        corpus_id=str(corpus_id),
                        chunk_id=str(chunk_id),
                        citation_id=str(citation_id),
                        score=score,
                        tokens=tokens,
                    )
                )
            if strict and len(results) == emitted_before:
                raise RuntimeError("retrieval_adapter_malformed_chunks")
        return results[:top_k]

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
        """Persist a correction to AIAR's grounding store.

        Returns True on success, False if AIAR is unavailable or the call
        failed. A stale AIAR grounding-store signature raises
        ``AiarGroundingRecordError`` rather than retrying positionally, because
        the old fallback wrote the answer into AIAR's correction/verdict slot
        and dropped instance scope.
        """
        try:
            from aiar.grounding import store as grounding_store  # type: ignore
        except Exception:
            return False
        normalized_verdict = schema_guard.normalize_verdict(verdict)
        signature = prompt_signature(prompt)
        try:
            grounding_store.record(  # type: ignore[attr-defined]
                signature=signature,
                verdict=normalized_verdict,
                correction=correction or "",
                instance=instance or None,
            )
        except TypeError as exc:
            raise AiarGroundingRecordError(
                "AIAR grounding store rejected the signature/verdict/"
                "correction/instance contract"
            ) from exc
        except Exception:
            return False
        return True


def _result_get(result: Any, key: str) -> Any:
    """Read a field from an AIAR result via dict-or-attribute access."""
    if isinstance(result, dict):
        return result.get(key)
    return getattr(result, key, None)


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


def _chunk_get(chunk: Any, key: str) -> Any:
    """Read a field from a retrieval chunk via dict-or-attribute access."""
    if isinstance(chunk, dict):
        return chunk.get(key)
    return getattr(chunk, key, None)


def _extract_retrieval_chunks(result: Any) -> list[Any]:
    """Pull a list of retrieved chunks out of AIAR's ``answer_prompt`` shape.

    AIAR's ``answer_prompt`` (today's editable-dev surface) returns a
    dict-like with the retrieved chunks under one of several keys
    depending on AIAR rev: ``retrieval.chunks``, ``retrieved``,
    ``chunks``, ``sources``. We probe in that order and return the first
    list-like we find. Returns ``[]`` if no recognized shape is present
    so the caller never iterates None.
    """
    if isinstance(result, dict):
        retrieval = result.get("retrieval")
        if isinstance(retrieval, dict):
            chunks = retrieval.get("chunks") or retrieval.get("sources")
            if isinstance(chunks, list):
                return chunks
        for key in ("retrieved", "chunks", "sources"):
            chunks = result.get(key)
            if isinstance(chunks, list):
                return chunks
    else:
        # Attribute-style result objects.
        retrieval = getattr(result, "retrieval", None)
        if retrieval is not None:
            chunks = getattr(retrieval, "chunks", None) or getattr(
                retrieval, "sources", None,
            )
            if isinstance(chunks, list):
                return chunks
        for key in ("retrieved", "chunks", "sources"):
            chunks = getattr(result, key, None)
            if isinstance(chunks, list):
                return chunks
    return []


def _extract_answer_and_verdict(result: Any) -> tuple[str, Any]:
    """Pull ``answer`` + raw verdict out of AIAR's version-dependent return shape."""
    if isinstance(result, dict):
        answer = (
            result.get("answer")
            or result.get("text")
            or result.get("output")
            or ""
        )
        verdict_raw = (
            result.get("verdict")
            or result.get("judge")
            or result.get("judgement")
        )
        return answer or "", verdict_raw
    return str(result), None
