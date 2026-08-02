"""SPEC-38/39 — the interaction gate must target the ball, and the mechanic phase
needs no render-loop pause.

Run 3 stalled because the SPEC-30 interaction gate fires a FIXED-location gesture
that misses the ball in a grab-to-aim control → interaction_changed=False red-flagged
a playable game forever. SPEC-38 targets the gesture at state().ball (isolating from
gravity drift via setMechanic) and judges by a real ball movement; a working hook +
a DEAD mouse must still RED (the dropped-S2 hole). SPEC-39 drops the spurious
"pause physics in the render loop" clause — proven by an always-stepping fixture.

Unit tests lock the fold (no S2 defer); the `live` tests drive the real
scripts/web-probe.mjs against control + adversarial fixtures (opt-in: `pytest -m live`).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from errorta_council.coding.web_probe import _verdict_to_result

_PW_BROWSERS_PATH = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
                     or str(Path.home() / "Library" / "Caches" / "ms-playwright"))
_FIX = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- #
# Unit — the interaction gate does NOT defer to the hook (dropped-S2 lock)
# --------------------------------------------------------------------------- #
def _v(interaction_changed, mp):
    return {"ok": True, "non_black": True, "console_errors": [],
            "interaction_changed": interaction_changed, "reason": "rendered",
            "mechanic_probe": mp}


def test_interaction_false_reds_even_with_working_hook() -> None:
    # THE dropped-S2 lock: a working hook (mechanic_matters=True) does NOT rescue a
    # dead pointer path — interaction_changed=False still reds the verdict.
    v = _v(False, {"has_hook": True, "ran": True, "mechanic_matters": True})
    assert _verdict_to_result(v, declares_mechanic=True).passed is False


def test_interaction_true_with_working_mechanic_passes() -> None:
    v = _v(True, {"has_hook": True, "ran": True, "mechanic_matters": True})
    assert _verdict_to_result(v, declares_mechanic=True).passed is True


# --------------------------------------------------------------------------- #
# LIVE — drive the real web-probe.mjs against control + adversarial fixtures
# --------------------------------------------------------------------------- #
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(directory):
    port = _free_port()
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def _probe(url):
    repo = Path(__file__).resolve().parents[3]
    mjs = repo / "scripts" / "web-probe.mjs"
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": _PW_BROWSERS_PATH}
    out = subprocess.run(["node", str(mjs), url], capture_output=True, text=True,
                         cwd=str(repo), timeout=90, env=env)
    for line in reversed(out.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON verdict; stdout={out.stdout!r} stderr={out.stderr[-400:]!r}")


@pytest.mark.live
def test_live_grabaim_passes_via_targeted_gesture() -> None:
    # SPEC-38: a grab-to-aim control (mousedown only launches if it lands on the
    # ball). The old blind gesture misses the ball; the ball-targeted gesture hits.
    if not shutil.which("node"):
        pytest.skip("node not available")
    httpd, port = _serve(_FIX / "spec38" / "grabaim")
    try:
        v = _probe(f"http://127.0.0.1:{port}/index.html")
    finally:
        httpd.shutdown()
    assert v.get("interaction_changed") is True, v
    assert _verdict_to_result(v, declares_mechanic=True).passed is True


@pytest.mark.live
def test_live_mousedead_reds_despite_working_hook() -> None:
    # THE hole the dropped S2 would open: renders, a fully working __probe hook
    # (mechanic_matters=True), but NO mouse listener → a human cannot play. Must RED.
    if not shutil.which("node"):
        pytest.skip("node not available")
    httpd, port = _serve(_FIX / "spec38" / "mousedead")
    try:
        v = _probe(f"http://127.0.0.1:{port}/index.html")
    finally:
        httpd.shutdown()
    mp = v.get("mechanic_probe") or {}
    assert mp.get("has_hook") is True and mp.get("mechanic_matters") is True, v
    assert v.get("interaction_changed") is False, v
    assert _verdict_to_result(v, declares_mechanic=True).passed is False
    assert "mouse control path" in str(v.get("reason") or ""), v.get("reason")


@pytest.mark.live
def test_live_alwaysstep_passes_without_pausing_the_loop() -> None:
    # SPEC-39: a game whose render loop NEVER pauses on probe control still passes
    # the mechanic phase — the differential is synchronous, so the loop is frozen
    # during it regardless. Proves the dropped pause clause was unnecessary.
    if not shutil.which("node"):
        pytest.skip("node not available")
    httpd, port = _serve(_FIX / "spec39" / "alwaysstep")
    try:
        v = _probe(f"http://127.0.0.1:{port}/index.html")
    finally:
        httpd.shutdown()
    mp = v.get("mechanic_probe") or {}
    assert mp.get("has_hook") is True and mp.get("mechanic_matters") is True, v
    assert _verdict_to_result(v, declares_mechanic=True).passed is True
