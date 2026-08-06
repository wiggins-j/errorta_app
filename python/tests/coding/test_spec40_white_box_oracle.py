"""SPEC-40 — the white-box acceptance oracle + the calibrated, advisory differential.

The testability contract became the dominant source of council churn because its
oracle is a black box with a hidden rubric: golf-2 found its false-PASS regime,
golf-3 and golf-4 two different false-RED regimes. This suite locks the rework:

* **item A** — the power sweep is calibrated to the GAME's usable range (bisect for
  the minimum power at which a mechanic-OFF straight shot sinks) instead of the hole
  geometry, which was 32-80x miscalibrated against a game whose ``shoot()`` takes a
  speed;
* **item B** — the mechanic differential no longer feeds the anchored ``web:probe``
  ``passed``, so a marginal verdict can never drive ``anchor_regressed`` /
  ``revise_livelock``;
* **item C** — both probe arms stamp the same components (the feedback-locality bug:
  22 green PRs merged against a weaker per-PR verdict while master stayed red);
* **item D** — the white-box ``solution()``/``won()`` phase, the primary verdict;
* **item E** — the four-path done-gate hierarchy.

Unit tests drive synthetic verdict dicts (no browser). The ``live`` tests drive the
real ``scripts/web-probe.mjs`` against fixtures (opt-in: ``pytest -m live``).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_PW_BROWSERS_PATH = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
                     or str(Path.home() / "Library" / "Caches" / "ms-playwright"))
_FIX = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- #
# Task 1 — the policy knobs
# --------------------------------------------------------------------------- #
def test_spec40_policy_knobs_default_on_and_roundtrip() -> None:
    from errorta_council.coding.autonomy import (
        CodingAutonomyPolicy, policy_from_dict, policy_to_dict)
    p = CodingAutonomyPolicy()
    assert p.probe_adaptive_sweep is True
    assert p.probe_mechanic_advisory is True
    assert p.probe_whitebox is True
    assert p.probe_pr_gating is True
    d = policy_to_dict(p)
    for k in ("probe_adaptive_sweep", "probe_mechanic_advisory",
              "probe_whitebox", "probe_pr_gating"):
        assert d[k] is True
    off = policy_from_dict({**d, "probe_whitebox": False})
    assert off.probe_whitebox is False
    assert off.probe_adaptive_sweep is True


# --------------------------------------------------------------------------- #
# Live-probe plumbing (shared with the SPEC-37/38 suites' pattern)
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(directory: Path):
    port = _free_port()
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def _probe(url: str) -> dict:
    repo = Path(__file__).resolve().parents[3]
    mjs = repo / "scripts" / "web-probe.mjs"
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": _PW_BROWSERS_PATH}
    out = subprocess.run(["node", str(mjs), url], capture_output=True, text=True,
                         cwd=str(repo), timeout=180, env=env)
    for line in reversed(out.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(
        f"no JSON verdict; stdout={out.stdout!r} stderr={out.stderr[-400:]!r}")


def _probe_fixture(*parts: str) -> dict:
    httpd, port = _serve(_FIX.joinpath(*parts))
    try:
        return _probe(f"http://127.0.0.1:{port}/")
    finally:
        httpd.shutdown()
