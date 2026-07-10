"""F039 slices 2-3 — web_fetch and web_search ToolGateway handlers.

Both are remote-egress tools (default-off, F041 first-use consent + ALLOW
required by the scheduler before they're ever invoked). They return normalized
ToolCallResult so output goes through the same hash-validation + side-store +
byte-isolation path as every other tool result. Output is DATA — the member
system prompt treats tool content as untrusted reference material.

httpx is imported lazily inside invoke() so importing this module (or the
registry) pulls no HTTP client into the import graph until a tool actually runs.
"""
from __future__ import annotations

import time
from typing import Any

from ..gateway import FatalToolError, RetryableToolError, ToolCallRequest, ToolCallResult
from .ssrf import (
    SsrfError,
    assert_fetch_url_allowed,
    pin_url_to_ip,
    resolve_validated_target,
)

_DEFAULT_MAX_BYTES = 2_000_000
_DEFAULT_TIMEOUT = 30
_MAX_REDIRECTS = 3
_DEFAULT_SEARCH_RESULTS = 5


def _sub_policy(request: ToolCallRequest, family: str) -> dict[str, Any]:
    tp = request.metadata.get("tool_policy")
    if isinstance(tp, dict) and isinstance(tp.get(family), dict):
        return tp[family]
    return {}


class WebFetchHandler:
    tool_id = "web_fetch"

    async def invoke(self, request: ToolCallRequest) -> ToolCallResult:
        import httpx

        url = str(request.arguments.get("url") or "").strip()
        if not url:
            raise FatalToolError("web_fetch_missing_url")
        policy = _sub_policy(request, "web_fetch")
        allowed = policy.get("allowed_domains") or []
        max_bytes = int(policy.get("max_bytes") or _DEFAULT_MAX_BYTES)
        timeout = float(policy.get("timeout_seconds") or _DEFAULT_TIMEOUT)

        start = time.monotonic()
        current = url
        try:
            # Manual redirect following. On EVERY hop we (1) re-run the SSRF
            # guard and (2) connect to the validated IP literal rather than let
            # httpx re-resolve the host — closing the DNS-rebinding window (a
            # low-TTL domain can't answer public to the guard then private to
            # httpx). The original hostname is preserved for TLS SNI + the Host
            # header so certificate validation is unaffected. (F086 Slice C.)
            async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
                resp = None
                for _ in range(_MAX_REDIRECTS + 1):
                    host, ips = resolve_validated_target(current, allowed_domains=allowed)
                    pinned_url, original_host = pin_url_to_ip(current, ips[0])
                    resp = await client.get(
                        pinned_url,
                        headers={"Host": original_host},
                        extensions={"sni_hostname": original_host},
                    )
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            break
                        current = _resolve_redirect(current, location)
                        continue
                    break
                else:
                    raise FatalToolError("web_fetch_too_many_redirects")
        except SsrfError as exc:
            raise FatalToolError(str(exc)) from None
        except httpx.TimeoutException:
            raise RetryableToolError("web_fetch_timeout") from None
        except httpx.HTTPError:
            raise RetryableToolError("web_fetch_transport_error") from None

        if resp is None:
            raise RetryableToolError("web_fetch_no_response")
        if resp.status_code >= 400:
            raise FatalToolError(f"web_fetch_http_{resp.status_code}")

        body = resp.content[:max_bytes]
        text = body.decode(resp.encoding or "utf-8", errors="replace")
        truncated = len(resp.content) > max_bytes
        if truncated:
            text += "\n[truncated]"
        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolCallResult.from_content(
            request=request,
            content=text,
            duration_ms=duration_ms,
            egress_class="remote",
            provenance={
                "final_url": current,
                "status": resp.status_code,
                "content_type": resp.headers.get("content-type", ""),
                "truncated": truncated,
            },
        )


class WebSearchHandler:
    tool_id = "web_search"

    async def invoke(self, request: ToolCallRequest) -> ToolCallResult:
        import os

        import httpx

        query = str(request.arguments.get("query") or "").strip()
        if not query:
            raise FatalToolError("web_search_missing_query")
        policy = _sub_policy(request, "web_search")
        endpoint = str(policy.get("searxng_url") or "").strip()
        if not endpoint:
            try:
                from errorta_app import settings as app_settings

                endpoint = app_settings.get_tools_settings().get("searxng_url", "")
            except Exception:  # noqa: BLE001 - settings failures should not block env fallback.
                endpoint = ""
        if not endpoint:
            endpoint = os.environ.get("ERRORTA_SEARXNG_URL", "").strip()
        if not endpoint:
            raise FatalToolError("web_search_not_configured")
        try:
            # The SearXNG endpoint is operator-configured trusted infrastructure
            # (commonly self-hosted on localhost/LAN), so the loopback/private
            # block is relaxed — but link-local/metadata stays blocked.
            assert_fetch_url_allowed(endpoint, allow_private_host=True)
        except SsrfError as exc:
            raise FatalToolError(f"web_search_{exc}") from None

        # Redact the outbound query (it may echo corpus-derived text) before it
        # leaves the machine.
        safe_query = _redact_query(query)
        n = int(policy.get("max_results") or _DEFAULT_SEARCH_RESULTS)
        search_url = endpoint.rstrip("/") + "/search"

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(
                follow_redirects=False, timeout=_DEFAULT_TIMEOUT
            ) as client:
                resp = await client.get(
                    search_url, params={"q": safe_query, "format": "json"}
                )
        except httpx.TimeoutException:
            raise RetryableToolError("web_search_timeout") from None
        except httpx.HTTPError:
            raise RetryableToolError("web_search_transport_error") from None
        if resp.status_code >= 400:
            raise FatalToolError(f"web_search_http_{resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise FatalToolError("web_search_bad_json") from None

        results = data.get("results") if isinstance(data, dict) else None
        lines: list[str] = []
        for item in (results or [])[:n]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            snippet = str(item.get("content") or "").strip()
            lines.append(f"- {title}\n  {url}\n  {snippet}")
        content = "\n".join(lines) if lines else "(no results)"
        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolCallResult.from_content(
            request=request,
            content=content,
            duration_ms=duration_ms,
            egress_class="remote",
            provenance={"result_count": len(lines), "backend": "searxng"},
        )


def _resolve_redirect(base: str, location: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base, location)


def _redact_query(query: str) -> str:
    try:
        from errorta_diagnostics import redact

        redacted, _ = redact.apply_pipeline(query)
        return redacted
    except Exception:
        return query


__all__ = ["WebFetchHandler", "WebSearchHandler"]
