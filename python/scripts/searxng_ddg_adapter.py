#!/usr/bin/env python3
"""Minimal SearXNG-compatible search adapter backed by DuckDuckGo (keyless).

Errorta's `web_search` tool speaks the SearXNG JSON contract:
    GET /search?q=<query>&format=json  ->  {"results": [{"title","url","content"}, ...]}

This stands up that exact endpoint locally without Docker or a full SearXNG,
fetching real results via the `ddgs` library. Point the sidecar at it with
    ERRORTA_SEARXNG_URL=http://127.0.0.1:8790
(or set tool_policy.web_search.searxng_url in the room).

Dev/test convenience only — DuckDuckGo rate-limits aggressive use.

Usage:  python scripts/searxng_ddg_adapter.py [--host 127.0.0.1] [--port 8790]
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover - clear operator error
    raise SystemExit("ddgs is not installed. Run: pip install ddgs")

_MAX_RESULTS = 8


def _search(query: str, limit: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=limit):
            out.append({
                "title": str(r.get("title") or ""),
                "url": str(r.get("href") or r.get("url") or ""),
                "content": str(r.get("body") or r.get("snippet") or ""),
            })
    return out


class _Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib signature)
        parts = urlsplit(self.path)
        if parts.path.rstrip("/") not in ("/search", ""):
            self._json(404, {"error": "not_found", "path": self.path})
            return
        params = parse_qs(parts.query)
        query = (params.get("q") or [""])[0].strip()
        if not query:
            self._json(200, {"results": []})
            return
        try:
            results = _search(query, _MAX_RESULTS)
            self._json(200, {"query": query, "number_of_results": len(results),
                             "results": results})
        except Exception as exc:  # surface as a 502 so the tool retries/falls back
            self._json(502, {"error": "search_failed", "detail": str(exc)})

    def log_message(self, *_args) -> None:  # quiet
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"[searxng-ddg-adapter] serving /search?q=&format=json on "
          f"http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
