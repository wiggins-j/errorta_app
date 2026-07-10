"""Pipeline adapter for raw AIAR service endpoints such as example-host."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

import httpx

from errorta_judge import schema_guard

from .models import AnswerResult, QueryResult, Retrieval, Verdict
from .signature import prompt_signature


class AiarServicePipelineError(RuntimeError):
    """Safe-to-display AIAR service pipeline error."""


class AiarServicePipeline:
    """Pipeline implementation backed directly by AIAR service HTTP APIs.

    This is distinct from ``RemoteHttpPipeline``: the latter proxies to a full
    remote Errorta sidecar exposing ``/judge/verdict``; this adapter targets the
    AIAR framework service contract (``/services/prompt`` and pure retrieve).
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout_s: float = 120.0,
        verify: bool = True,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = float(timeout_s)
        self.verify = bool(verify)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _redact(self, text: str) -> str:
        if self.token and self.token in text:
            return text.replace(self.token, "<redacted>")
        return text

    def _request(self, method: str, path: str, *, json: Any | None = None) -> dict[str, Any]:
        try:
            with httpx.Client(
                timeout=httpx.Timeout(self.timeout_s),
                verify=self.verify,
                headers=self._headers(),
            ) as client:
                resp = client.request(method, f"{self.base_url}{path}", json=json)
        except (httpx.HTTPError, OSError) as exc:
            raise AiarServicePipelineError(
                f"AIAR service unreachable: {self._redact(str(exc))}"
            ) from None
        if not 200 <= resp.status_code < 300:
            detail = resp.text[:400]
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    detail = str(parsed.get("detail") or parsed)
            except ValueError:
                pass
            raise AiarServicePipelineError(
                f"AIAR service returned {resp.status_code}: {self._redact(detail)}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise AiarServicePipelineError(f"AIAR service returned invalid JSON: {exc}") from None
        if not isinstance(data, dict):
            raise AiarServicePipelineError("AIAR service returned a non-object response")
        return data

    def answer(
        self,
        *,
        prompt: str,
        corpus: str,
        judge: bool,
        reground: bool,
        model: Optional[str],
        top_k: int = 4,
    ) -> AnswerResult:
        body: dict[str, Any] = {
            "service_name": "errorta-judge",
            "prompt": prompt,
            "rag": bool(corpus),
            "judge": bool(judge),
            "think": False,
            "sources": True,
            "top_k": int(top_k),
        }
        if corpus:
            body["instance"] = corpus
        if model:
            body["model"] = model
        payload = self._request("POST", "/services/prompt", json=body)

        raw_verdict = (
            payload.get("verdict")
            or payload.get("judge")
            or payload.get("evaluation")
            or payload.get("eval")
        )
        verdict_obj: Verdict | None = None
        if raw_verdict is not None:
            norm = schema_guard.normalize_verdict(raw_verdict)
            verdict_obj = Verdict(
                rating=str(norm.get("rating") or "fail"),
                reason=str(norm.get("reason") or ""),
                failure_tags=list(norm.get("failure_tags") or []),
                confidence=norm.get("confidence"),
                usable="judge_unparseable" not in (norm.get("failure_tags") or []),
            )
        elif judge:
            verdict_obj = Verdict(
                rating="fail",
                reason="The selected AIAR service did not return a judge verdict.",
                failure_tags=["judge_unavailable"],
                confidence=None,
                usable=False,
            )
            raw_verdict = verdict_obj.to_dict()

        sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
        grounded = _optional_bool(payload.get("grounded"))
        reground_applied = _optional_bool(payload.get("reground_applied"))
        result = AnswerResult(
            answer=str(payload.get("answer") or payload.get("text") or ""),
            model=_optional_str(payload.get("model")) or model,
            verdict=verdict_obj,
            retrieval=Retrieval(
                grounded=grounded if grounded is not None else bool(sources),
                reground_applied=reground_applied if reground_applied is not None else False,
                top_k=int(payload.get("top_k") or 4),
                chunks_used=len(sources),
            ),
            prompt_signature=payload.get("prompt_signature") or prompt_signature(prompt),
            aiar=True,
            call_id=_optional_str(payload.get("call_id")),
            instance=_optional_str(payload.get("instance")) or (corpus or None),
            grounded=grounded,
            reground_applied=reground_applied,
            rag_enabled=_optional_bool(payload.get("rag_enabled")) or bool(corpus),
            latency=_optional_float(payload.get("latency") or payload.get("latency_ms")),
        )
        if raw_verdict is not None:
            result.raw_verdict = raw_verdict  # type: ignore[attr-defined]
        return result

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
        out: list[QueryResult] = []
        for corpus_id in corpus_ids:
            path = (
                f"/instances/{quote(corpus_id, safe='')}/retrieve"
                f"?q={quote(prompt, safe='')}&k={int(top_k)}"
            )
            try:
                payload = self._request("GET", path)
            except AiarServicePipelineError:
                if strict:
                    raise
                continue
            instance = str(payload.get("instance") or corpus_id)
            for item in payload.get("hits") or []:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "")
                if not text:
                    continue
                score = item.get("score")
                page_span = item.get("page_span")
                out.append(
                    QueryResult(
                        content=text,
                        corpus_id=instance,
                        chunk_id=str(item.get("chunk_id") or ""),
                        citation_id=str(item.get("citation_id") or item.get("chunk_id") or ""),
                        score=score if isinstance(score, (int, float)) else None,
                        source=_optional_str(item.get("source")),
                        title=_optional_str(item.get("title")),
                        page_span=_page_span(page_span),
                        metadata={
                            k: v
                            for k, v in item.items()
                            if k not in {"text", "chunk_id", "citation_id", "score"}
                        },
                    )
                )
        return out

    def record_grounding(
        self,
        *,
        prompt: str,
        answer: str,
        correction: str | None,
        verdict: dict[str, Any] | None,
        instance: str | None = None,
    ) -> bool:
        # AIAR grounding record routes are still formalized in F096. Fail closed
        # for remote service writes instead of inventing an endpoint.
        return False


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_bool(value: Any) -> bool | None:
    return bool(value) if isinstance(value, bool) else None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _page_span(value: Any) -> tuple[int, int] | None:
    if (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(isinstance(x, int) and not isinstance(x, bool) for x in value)
    ):
        return int(value[0]), int(value[1])
    return None
