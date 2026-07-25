"""GL01 (Item 1) — a @playwright-gated smoke test for the Node probe script itself.

This exercises the REAL ``scripts/web-probe.mjs`` against a real ``http.server`` —
the one place the actual Chromium/Playwright path is covered. It SKIPS cleanly when
node, the Playwright package, or a Chromium browser is unavailable (the CI posture:
the coding suite runs with no browser installed), so the engine-integration tests
never depend on it. When the toolchain IS present, it locks the oracle end-to-end:
a live coloured canvas is non-black; a zero-size canvas is black.
"""
from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "web-probe.mjs"

_GOOD = ("<html><body><canvas id=c></canvas><script>"
         "const c=document.getElementById('c');c.width=300;c.height=150;"
         "const x=c.getContext('2d');x.fillStyle='#3af';x.fillRect(0,0,300,150);"
         "</script></body></html>")
_BLACK = ("<html><body><canvas id=c></canvas><script>"
          "const c=document.getElementById('c');c.width=0;c.height=0;"
          "</script></body></html>")


def _playwright_available() -> bool:
    if shutil.which("node") is None or not _SCRIPT.exists():
        return False
    check = ("Promise.any(['@playwright/test','playwright','playwright-core']"
             ".map(m=>import(m).then(x=>{if(!x.chromium)throw 0;return x.chromium"
             ".executablePath()})) ).then(async p=>{const fs=await import('node:fs');"
             "fs.accessSync(p);process.exit(0)}).catch(()=>process.exit(1))")
    try:
        r = subprocess.run(["node", "--input-type=module", "-e", check],
                           cwd=str(_REPO_ROOT), capture_output=True,
                           timeout=60, check=False)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _playwright_available(),
    reason="node + Playwright + a Chromium browser are required for the probe smoke")


@contextlib.contextmanager
def _serve(html: str, tmp_path: Path):
    (tmp_path / "index.html").write_text(html, "utf-8")
    handler = partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        httpd.shutdown()


def _probe(url: str) -> dict:
    r = subprocess.run(["node", str(_SCRIPT), url, "10"], cwd=str(_REPO_ROOT),
                       capture_output=True, text=True, timeout=90, check=False)
    line = [ln for ln in r.stdout.splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(line)


def test_live_canvas_is_non_black(tmp_path: Path) -> None:
    with _serve(_GOOD, tmp_path) as url:
        v = _probe(url)
    assert v["non_black"] is True and v["ok"] is True
    assert v["console_errors"] == []


def test_zero_size_canvas_is_black(tmp_path: Path) -> None:
    with _serve(_BLACK, tmp_path) as url:
        v = _probe(url)
    assert v["non_black"] is False and v["ok"] is False
