"""Live validation of F039 tools — real internet, real subprocess, real models.

Run: ERRORTA_OLLAMA_HOST=http://127.0.0.1:11435 python scripts/validate_f039_live.py

web_search: by default this stands up a local stub server speaking the SearXNG
``/search?format=json`` contract, so the handler's request/parse/redaction path
runs over a real socket without external infra. To validate against a REAL
SearXNG instead, run one and point the env var at it, e.g.:

    docker run --rm -p 8888:8080 searxng/searxng
    ERRORTA_SEARXNG_URL=http://localhost:8888 python scripts/validate_f039_live.py

seatbelt: the macOS sandbox step runs only when ``sandbox-exec`` is present
(it is, on every Mac); it is skipped cleanly elsewhere.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import validate_council_live as V  # reuse member/room/run_case helpers

from errorta_tools.builtins.code_exec import CodeExecHandler
from errorta_tools.builtins.ssrf import SsrfError, assert_fetch_url_allowed
from errorta_tools.builtins.web import WebFetchHandler, WebSearchHandler
from errorta_tools.gateway import FatalToolError, ToolCallRequest
from errorta_tools.runner import sandbox as _sb


class _StubSearxng(BaseHTTPRequestHandler):
    """A minimal server speaking the SearXNG /search?format=json contract.

    Records the `q` it was asked (so the test can assert the query was
    redacted before egress) and returns two canned results.
    """

    received_q: list[str] = []

    def log_message(self, *_a):  # silence stderr access log
        pass

    def do_GET(self):  # noqa: N802
        parts = urlsplit(self.path)
        if not parts.path.endswith("/search"):
            self.send_response(404)
            self.end_headers()
            return
        q = parse_qs(parts.query).get("q", [""])[0]
        type(self).received_q.append(q)
        body = json.dumps({
            "results": [
                {"title": "Result one", "url": "https://example.org/a",
                 "content": "first snippet"},
                {"title": "Result two", "url": "https://example.org/b",
                 "content": "second snippet"},
            ]
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextlib.contextmanager
def _stub_searxng():
    _StubSearxng.received_q = []
    httpd = HTTPServer(("127.0.0.1", 0), _StubSearxng)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", _StubSearxng
    finally:
        httpd.shutdown()
        thread.join(timeout=3)


def _req(tool_id, arguments, tool_policy):
    return ToolCallRequest(
        call_id="tc-live", run_id="run-live", turn_id="t-1", member_id="m-1",
        tool_id=tool_id, arguments=arguments,
        metadata={"round": 1, "tool_policy": tool_policy},
    )


async def main():
    results = []

    # 1. web_fetch against the REAL internet.
    r = await WebFetchHandler().invoke(
        _req("web_fetch", {"url": "https://example.com/"}, {"web_fetch": {"enabled": True}})
    )
    fetched_ok = "example" in r.content.lower() and r.egress_class == "remote"
    print(f"\n=== web_fetch LIVE (https://example.com) ===\n  egress={r.egress_class} "
          f"bytes~{len(r.content)} contains_example={fetched_ok}")
    results.append(("web_fetch fetches real internet", fetched_ok))

    # 2. SSRF guard blocks a real private URL.
    blocked = False
    try:
        await WebFetchHandler().invoke(
            _req("web_fetch", {"url": "http://169.254.169.254/latest/meta-data/"},
                 {"web_fetch": {"enabled": True}})
        )
    except FatalToolError as e:
        blocked = "ssrf" in str(e)
    print(f"=== SSRF guard (cloud metadata) ===\n  blocked={blocked}")
    results.append(("SSRF blocks metadata endpoint", blocked))
    # ...and the guard rejects loopback directly.
    try:
        assert_fetch_url_allowed("http://127.0.0.1/")
        results.append(("SSRF blocks loopback", False))
    except SsrfError:
        results.append(("SSRF blocks loopback", True))

    # 3. code_exec runs a REAL subprocess in a workspace.
    ws = Path(tempfile.mkdtemp())
    (ws / "calc.py").write_text("print('result=', 2 + 2)\n")
    res = await CodeExecHandler().invoke(
        _req("code_exec", {"argv": [sys.executable, "calc.py"]},
             {"code_read": {"enabled": True, "workspace_path": str(ws)},
              "code_exec": {"enabled": True}, "execution": {"location": "local"}})
    )
    import json
    payload = json.loads(res.content)
    exec_ok = payload["exit_code"] == 0 and "result= 4" in payload["stdout_preview"]
    print(f"=== code_exec LIVE (real subprocess) ===\n  exit={payload['exit_code']} "
          f"stdout={payload['stdout_preview']!r} ok={exec_ok}")
    results.append(("code_exec runs real subprocess", exec_ok))

    # 4. web_search over REAL HTTP. Prefer a real SearXNG via ERRORTA_SEARXNG_URL;
    #    otherwise stand up a local stub speaking the SearXNG JSON contract. Either
    #    way the handler's request/parse/redaction path runs over a real socket.
    real_searxng = os.environ.get("ERRORTA_SEARXNG_URL", "").strip()
    secret_query = "find docs for sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX usage"
    if real_searxng:
        r = await WebSearchHandler().invoke(
            _req("web_search", {"query": secret_query},
                 {"web_search": {"enabled": True, "searxng_url": real_searxng}})
        )
        search_ok = r.egress_class == "remote" and r.provenance.get("result_count", 0) >= 0
        print(f"=== web_search LIVE (real SearXNG {real_searxng}) ===\n"
              f"  egress={r.egress_class} results={r.provenance.get('result_count')}")
        results.append(("web_search hits real SearXNG", search_ok))
    else:
        with _stub_searxng() as (url, stub):
            r = await WebSearchHandler().invoke(
                _req("web_search", {"query": secret_query},
                     {"web_search": {"enabled": True, "searxng_url": url}})
            )
            parsed_ok = (
                r.egress_class == "remote"
                and r.provenance.get("result_count") == 2
                and "Result one" in r.content
                and "https://example.org/a" in r.content
            )
            # The secret token must NOT have reached the (real-socket) endpoint;
            # the redaction placeholder must be what the server saw.
            seen = stub.received_q[-1] if stub.received_q else ""
            redacted_ok = "sk-ant-api03" not in seen and "<token-redacted>" in seen
            print(f"=== web_search LIVE (local stub SearXNG) ===\n"
                  f"  results={r.provenance.get('result_count')} parsed={parsed_ok}\n"
                  f"  query_seen_by_server={seen!r} redacted={redacted_ok}")
            results.append(("web_search parses SearXNG JSON over real HTTP", parsed_ok))
            results.append(("web_search redacts query before egress", redacted_ok))

    # 4b. seatbelt sandbox (macOS) — REAL OS enforcement of network + writes.
    if _sb.is_available(_sb.SANDBOX_SEATBELT):
        sbx = Path(tempfile.mkdtemp())
        (sbx / "net.py").write_text(
            "import socket,sys\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1',53),timeout=3).close()\n"
            "    print('CONNECTED'); sys.exit(0)\n"
            "except Exception:\n"
            "    print('BLOCKED'); sys.exit(7)\n"
        )
        res = await CodeExecHandler().invoke(
            _req("code_exec", {"argv": [sys.executable, "net.py"]},
                 {"code_read": {"enabled": True, "workspace_path": str(sbx)},
                  "code_exec": {"enabled": True},
                  "execution": {"location": "local", "sandbox": "seatbelt"}})
        )
        p = json.loads(res.content)
        sbx_ok = p["exit_code"] != 0 and "CONNECTED" not in (p.get("stdout_preview") or "")
        print(f"=== seatbelt sandbox LIVE (deny network) ===\n"
              f"  exit={p['exit_code']} stdout={p.get('stdout_preview')!r} blocked={sbx_ok}")
        results.append(("seatbelt sandbox blocks network (real OS)", sbx_ok))
    else:
        print("=== seatbelt sandbox: skipped (sandbox-exec unavailable) ===")

    # 5. build_review on REAL example-host models (Gemma programmer + Mistral reviewer).
    base = Path(tempfile.mkdtemp())
    room = V.room(
        "live-build-review",
        [V.member("prog", V.GEMMA, role="programmer", system_prompt=(
            "You are the programmer. Propose a tiny Python function in a few "
            "lines. Be brief.")),
         V.member("rev1", V.MISTRAL, trans="all_messages", system_prompt=(
            "You are a code reviewer. If the programmer's proposal is "
            "reasonable, reply with EXACTLY: LGTM. Otherwise explain what to "
            "change."))],
    )
    room["topology"] = {"kind": "build_review", "max_iterations": 3,
                        "speaker_order": ["prog", "rev1"]}
    status, reason, nmsgs, has_fa, _ = await V.run_case(
        "build_review / Gemma programmer + Mistral reviewer / example-host",
        room, "Write a Python function that returns the nth Fibonacci number.",
        rounds=6, msgs=6, runs_dir=base / "br",
    )
    br_ok = status == "completed"
    print(f"=== build_review LIVE ===\n  status={status} reason={reason!r} messages={nmsgs}")
    results.append(("build_review completes on real models", br_ok))

    print("\n========== F039 LIVE VALIDATION ==========")
    allok = True
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        allok = allok and ok
    print("=" * 42)
    print("ALL F039 LIVE CHECKS OK" if allok else "SOME F039 LIVE CHECKS FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    asyncio.run(main())
