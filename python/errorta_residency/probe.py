"""F-INFRA-12 Phase B Slice 2 — HTTPS upstream probe + URL validator.

``probe_https_url`` is the workhorse for the residency Settings panel's
"Test Connection" button and the cloud-mode persistence guard: it does a
short-timeout GET against ``{url}/healthz`` (optionally carrying an
``X-Errorta-Token`` header) and returns a structured
``{ok, status, body, error}`` dict without ever raising.

``validate_https_url`` is the cheap shape-check used inline by the
``PUT /residency`` route to reject obvious garbage (``http://``,
missing netloc, empty strings) before any network call.

Network library: ``httpx.Client`` so it shares the FastAPI dep tree
without adding ``requests`` as a new direct dependency.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse

import httpx


def validate_https_url(raw: str) -> str:
    """Return ``raw.strip()`` if it parses as an ``https://`` URL with a netloc.

    Raises ``ValueError`` with a user-readable message on failure. The
    cleaned (whitespace-stripped) URL is returned on success so callers
    can persist the canonical form.
    """
    if not isinstance(raw, str):
        raise ValueError("url must be a string")
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("url must not be empty")
    if not cleaned.lower().startswith("https://"):
        raise ValueError("url must start with https:// (got non-https scheme)")
    try:
        parsed = urlparse(cleaned)
    except ValueError as exc:  # pragma: no cover — urlparse rarely raises
        raise ValueError(f"url is malformed: {exc}") from exc
    if parsed.scheme.lower() != "https":
        raise ValueError("url must start with https:// (got non-https scheme)")
    if not parsed.netloc:
        raise ValueError("url is missing a host")
    return cleaned


def probe_https_url(
    url: str,
    *,
    token: Optional[str] = None,
    timeout_s: float = 2.0,
) -> dict[str, Any]:
    """GET ``{url}/healthz`` and return a structured result.

    Returns ``{"ok": bool, "status": int|None, "body": dict|None,
    "error": str|None}``. Never raises: ``httpx.HTTPError``, ``OSError``,
    and ``ValueError`` are all swallowed into ``ok=False`` with the
    exception's message in ``error``.

    The token, when supplied, is sent as ``X-Errorta-Token``. It is
    intentionally not echoed back in the result so callers can log the
    dict without leaking credentials.
    """
    # Build the /healthz URL. ``url`` may or may not end in ``/`` — strip
    # one trailing slash so we don't end up with ``//healthz``.
    base = (url or "").rstrip("/")
    if not base:
        return {"ok": False, "status": None, "body": None, "error": "url must not be empty"}
    target = f"{base}/healthz"

    headers: dict[str, str] = {}
    if token:
        headers["X-Errorta-Token"] = token

    try:
        with httpx.Client(timeout=timeout_s) as client:
            response = client.get(target, headers=headers)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return {"ok": False, "status": None, "body": None, "error": str(exc)}

    status = getattr(response, "status_code", None)
    body: Optional[dict[str, Any]]
    try:
        parsed = response.json()
        body = parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        body = None

    ok = isinstance(status, int) and 200 <= status < 300
    return {
        "ok": ok,
        "status": status,
        "body": body,
        "error": None if ok else f"upstream returned {status}",
    }
