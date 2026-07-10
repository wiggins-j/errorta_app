"""Service API token guard for `/services/*` routes."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from . import store
from .ratelimit import RateLimited, auth_failure_limiter

TOKEN_HEADER = "x-errorta-token"


def _source_key(request: Request) -> str:
    return (request.client.host if request.client else "").strip().lower() or "unknown"


def _service_token(request: Request) -> str:
    return request.headers.get(TOKEN_HEADER, "").strip()


def _rate_limit_error(exc: RateLimited) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail="auth_rate_limited",
        headers={"Retry-After": str(max(1, exc.retry_after_seconds))},
    )


def require_service_token(
    request: Request,
    *,
    corpus: str | None = None,
    required_scope: str,
) -> dict[str, Any]:
    """Validate an `X-Errorta-Token` header and return the token metadata.

    The raw token is never logged or persisted here. `store.find_by_token()`
    hashes the presented value and compares with `hmac.compare_digest`, while
    revoked and unknown tokens collapse to the same public `token_revoked`
    response.
    """

    source = _source_key(request)
    try:
        auth_failure_limiter.check(source)
    except RateLimited as exc:
        raise _rate_limit_error(exc) from exc

    raw_token = _service_token(request)
    if not raw_token:
        auth_failure_limiter.record_failure(source)
        raise HTTPException(status_code=401, detail="token_missing")

    record = store.find_by_token(raw_token)
    if record is None:
        auth_failure_limiter.record_failure(source)
        raise HTTPException(status_code=401, detail="token_revoked")

    scopes = {str(item) for item in record.get("scopes") or []}
    if required_scope not in scopes:
        auth_failure_limiter.record_failure(source)
        raise HTTPException(status_code=403, detail="token_scope_denied")

    requested_corpus = (corpus or "").strip()
    if requested_corpus:
        allowed_corpora = {str(item) for item in record.get("corpora") or []}
        if requested_corpus not in allowed_corpora:
            auth_failure_limiter.record_failure(source)
            raise HTTPException(status_code=403, detail="token_corpus_denied")

    auth_failure_limiter.record_success(source)
    updated = dict(record)
    refreshed = store.update_last_used(str(record["id"]))
    if refreshed is not None:
        updated = refreshed
    return updated


__all__ = ["TOKEN_HEADER", "require_service_token"]
