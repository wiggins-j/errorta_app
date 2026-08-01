"""SPEC-37 — a behavioral mechanic oracle: prove a declared mechanic has EFFECT.

gravity-golf shipped inert gravity to definition_of_done in run-10 AND run-11: it
renders + responds to input (the liveness gate passes), but no oracle measured that
the declared mechanic changes outcomes. This adds that oracle on the web:probe's
trusted headless chromium: fire a STRAIGHT shot at the hole swept across powers; if
it SINKS a "non-trivial" level, the mechanic is inert. The naive path-curvature
threshold is deliberately NOT used (it false-passed the delivered inert game at
power 100 and false-failed a 10x-stronger build — see SPEC-37).

Unit tests cover the Python fold/scoping; the `live` tests drive the real
scripts/web-probe.mjs against control fixtures (opt-in: `pytest -m live`).
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

from errorta_council.coding.web_probe import (
    _declares_load_bearing_mechanic,
    _mechanic_verdict,
    _probe_verdict_fields,
    _verdict_to_result,
)

# Captured at IMPORT time — the coding conftest's autouse fixture remaps $HOME to a
# tmp dir per test, which would send the node subprocess's Playwright to an empty
# browser cache. Resolve the REAL cache here (before any fixture runs) so the live
# probe finds the installed Chromium.
_PW_BROWSERS_PATH = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
                     or str(Path.home() / "Library" / "Caches" / "ms-playwright"))

_FIX = Path(__file__).parent / "fixtures" / "spec37"


# --------------------------------------------------------------------------- #
# north-star signal — declares a load-bearing, non-trivial mechanic?
# --------------------------------------------------------------------------- #
class _Proj:
    def __init__(self, north_star="", dod=""):
        self.north_star = north_star
        self.definition_of_done = dod


class _Store:
    def __init__(self, proj):
        self._p = proj

    def get_project(self):
        return self._p


_GRAVITY_GOLF_DOD = ("At least 8 levels, each genuinely non-trivial (none solvable "
                     "by a straight line or in zero strokes; gravity must matter).")


def test_declares_needs_both_a_mechanic_and_a_nontriviality_term() -> None:
    assert _declares_load_bearing_mechanic(_Store(_Proj(dod=_GRAVITY_GOLF_DOD)))
    # gravity but no non-triviality claim -> not load-bearing (a straight shot may
    # legitimately sink), so the oracle must NOT gate.
    assert not _declares_load_bearing_mechanic(
        _Store(_Proj(dod="A relaxing gravity sandbox to play with.")))
    # non-triviality words but no mechanic -> a plain puzzle, not a force game.
    assert not _declares_load_bearing_mechanic(
        _Store(_Proj(dod="Levels are non-trivial; no straight-line solution.")))
    assert not _declares_load_bearing_mechanic(_Store(_Proj(dod="Ship a fun game.")))


def test_declares_fail_open_on_error() -> None:
    class _Bad:
        def get_project(self):
            raise RuntimeError("boom")
    assert _declares_load_bearing_mechanic(_Bad()) is False


# --------------------------------------------------------------------------- #
# _mechanic_verdict — the fold, gated on declares_mechanic
# --------------------------------------------------------------------------- #
def _v(mp):
    return {"ok": True, "non_black": True, "mechanic_probe": mp}


def test_mechanic_verdict_not_gated_when_not_declared() -> None:
    ok, _ = _mechanic_verdict(_v({"has_hook": False}), declares_mechanic=False)
    assert ok is True  # a game that didn't declare a load-bearing mechanic is free


def test_mechanic_verdict_no_hook_fails_and_names_contract() -> None:
    ok, reason = _mechanic_verdict(_v({"has_hook": False}), declares_mechanic=True)
    assert ok is False
    assert "window.__probe" in reason and "no scriptable state hook" in reason


def test_mechanic_verdict_straight_shot_sank_fails() -> None:
    ok, reason = _mechanic_verdict(
        _v({"has_hook": True, "ran": True, "straight_shot_sank": True}),
        declares_mechanic=True)
    assert ok is False and "inert" in reason


def test_mechanic_verdict_straight_shot_missed_ok() -> None:
    ok, _ = _mechanic_verdict(
        _v({"has_hook": True, "ran": True, "straight_shot_sank": False}),
        declares_mechanic=True)
    assert ok is True


def test_mechanic_verdict_partial_hook_is_fail_open() -> None:
    # a hook present but state unreadable (ran=False) must not be a false red.
    ok, _ = _mechanic_verdict(
        _v({"has_hook": True, "ran": False}), declares_mechanic=True)
    assert ok is True


# --------------------------------------------------------------------------- #
# fold into the recorded result + PR fields
# --------------------------------------------------------------------------- #
def _verdict(mp, ok=True, non_black=True, interaction_changed=True):
    return {"ok": ok, "non_black": non_black, "console_errors": [],
            "interaction_changed": interaction_changed, "reason": "rendered",
            "mechanic_probe": mp}


def test_result_blocks_declared_inert_game() -> None:
    v = _verdict({"has_hook": True, "ran": True, "straight_shot_sank": True})
    assert _verdict_to_result(v, declares_mechanic=True).passed is False
    # but the SAME verdict passes when the project didn't declare non-triviality
    assert _verdict_to_result(v, declares_mechanic=False).passed is True


def test_result_blocks_declared_no_hook_game() -> None:
    v = _verdict({"has_hook": False})
    r = _verdict_to_result(v, declares_mechanic=True)
    assert r.passed is False and "window.__probe" in r.stderr_preview


def test_result_passes_declared_live_game() -> None:
    v = _verdict({"has_hook": True, "ran": True, "straight_shot_sank": False})
    assert _verdict_to_result(v, declares_mechanic=True).passed is True


def test_result_unchanged_for_non_declared_projects() -> None:
    # regression: a project that declares no mechanic behaves exactly as pre-SPEC-37
    # (passed depends only on ok/non_black/console/interaction).
    v = _verdict({"has_hook": False})
    assert _verdict_to_result(v, declares_mechanic=False).passed is True


def test_pr_fields_fold_mechanic() -> None:
    v = _verdict({"has_hook": True, "ran": True, "straight_shot_sank": True})
    f = _probe_verdict_fields(v, head="h1", declares_mechanic=True)
    assert f["probe_passed"] is False and f["probe_mechanic_ok"] is False
    assert f["probe_mechanic_has_hook"] is True


# --------------------------------------------------------------------------- #
# LIVE — drive the real web-probe.mjs against control fixtures (opt-in)
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
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def _probe(url):
    repo = Path(__file__).resolve().parents[3]
    mjs = repo / "scripts" / "web-probe.mjs"
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": _PW_BROWSERS_PATH}
    out = subprocess.run(["node", str(mjs), url], capture_output=True, text=True,
                         cwd=str(repo), timeout=60, env=env)
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.live
@pytest.mark.parametrize("fixture,expect_sank,expect_hook", [
    ("live", False, True),      # off-axis strong gravity: straight shot MISSES
    ("inert", True, True),      # run-11 magnitude: straight shot SINKS
    ("nohook", None, False),    # a live game that exposes no hook
])
def test_live_mechanic_probe(fixture, expect_sank, expect_hook) -> None:
    if not shutil.which("node"):
        pytest.skip("node not available")
    httpd, port = _serve(_FIX / fixture)
    try:
        v = _probe(f"http://127.0.0.1:{port}/index.html")
    finally:
        httpd.shutdown()
    mp = v.get("mechanic_probe") or {}
    assert mp.get("has_hook") is expect_hook, v
    if expect_hook:
        assert mp.get("straight_shot_sank") is expect_sank, mp
    # end-to-end fold: a declared-mechanic project blocks inert + no-hook, passes live
    r = _verdict_to_result(v, declares_mechanic=True)
    should_pass = (fixture == "live")
    assert r.passed is should_pass, (fixture, r.stderr_preview)
