"""SPEC-37 — a behavioral mechanic oracle: prove a declared mechanic has EFFECT.

gravity-golf shipped inert gravity to definition_of_done in run-10 AND run-11: it
renders + responds to input (the liveness gate passes), but no oracle measured that
the declared mechanic changes outcomes. This adds a DIFFERENTIAL oracle on the
web:probe's trusted headless chromium: via a window.__probe hook, fire the SAME
straight shot at the hole with the mechanic ON vs OFF; if the outcome is identical
at every power, the mechanic is inert. Differential (not "a straight shot must
miss") because an absolute win-condition depends on the game's unknown power cap +
hole geometry — an adversarial stress pass disproved that form (SPEC-37).

Unit tests cover the Python signal/fold; the `live` tests drive the real
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

from errorta_council.coding.web_probe import (
    _declares_load_bearing_mechanic,
    _mechanic_verdict,
    _probe_verdict_fields,
    _verdict_to_result,
)

# Captured at IMPORT time — the coding conftest's autouse fixture remaps $HOME per
# test, which would send the node subprocess's Playwright to an empty browser cache.
_PW_BROWSERS_PATH = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
                     or str(Path.home() / "Library" / "Caches" / "ms-playwright"))

_FIX = Path(__file__).parent / "fixtures" / "spec37"


# --------------------------------------------------------------------------- #
# north-star signal — declares a mechanic AND forbids straight-line solutions?
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


def test_declares_true_for_gravity_golf() -> None:
    assert _declares_load_bearing_mechanic(_Store(_Proj(dod=_GRAVITY_GOLF_DOD)))


def test_declares_false_when_no_straight_fail_claim() -> None:
    # gravity present but nothing forbids a straight solution -> not gated.
    assert not _declares_load_bearing_mechanic(
        _Store(_Proj(dod="A relaxing gravity sandbox to play with.")))


def test_declares_false_for_non_mechanic_project() -> None:
    # non-triviality words but no PHYSICS mechanic -> a plain puzzle, not gated.
    assert not _declares_load_bearing_mechanic(
        _Store(_Proj(dod="Levels are non-trivial; no straight-line solution.")))


def test_declares_no_false_match_on_ordinary_words() -> None:
    # REVIEW LOCK: an earlier draft matched the bare substrings "well"/"force"/
    # "must matter", so a CRUD app "handles validation well ... non-trivial" was
    # hard-blocked. Word/phrase boundaries + a real mechanic term prevent that.
    assert not _declares_load_bearing_mechanic(_Store(_Proj(
        dod="A non-trivial form app that handles validation well and feels great.")))
    assert not _declares_load_bearing_mechanic(_Store(_Proj(
        dod="Force a clean, non-trivial UX; the CTA must matter to users.")))
    # RE-REVIEW LOCK: a physics project that never forbids straight-line solutions
    # must NOT be gated just because it pairs a physics term with "non-trivial".
    assert not _declares_load_bearing_mechanic(_Store(_Proj(
        dod="A non-trivial physics particle sandbox / screensaver.")))
    assert not _declares_load_bearing_mechanic(_Store(_Proj(
        dod="An orbit-based puzzle; each level should feel non-trivial.")))


def test_declares_fail_open_on_error() -> None:
    class _Bad:
        def get_project(self):
            raise RuntimeError("boom")
    assert _declares_load_bearing_mechanic(_Bad()) is False


# --------------------------------------------------------------------------- #
# _mechanic_verdict — the fold, gated on declares_mechanic
# --------------------------------------------------------------------------- #
def _v(mp=None, omit=False):
    d = {"ok": True, "non_black": True}
    if not omit:
        d["mechanic_probe"] = mp if mp is not None else {}
    return d


def test_verdict_not_gated_when_not_declared() -> None:
    assert _mechanic_verdict(_v({"has_hook": False}), declares_mechanic=False)[0] is True


def test_verdict_advisory_when_phase_absent() -> None:
    # a probe error emits no mechanic_probe key -> cannot-verify, never a false red.
    ok, reason = _mechanic_verdict(_v(omit=True), declares_mechanic=True)
    assert ok is True and reason == ""


def test_verdict_no_hook_fails_and_names_contract() -> None:
    ok, reason = _mechanic_verdict(_v({"has_hook": False}), declares_mechanic=True)
    assert ok is False and "window.__probe" in reason and "setMechanic" in reason


def test_verdict_unusable_hook_fails() -> None:
    ok, reason = _mechanic_verdict(
        _v({"has_hook": True, "ran": False, "reason": "shoot() did not move"}),
        declares_mechanic=True)
    assert ok is False and "unusable" in reason


def test_verdict_inert_fails() -> None:
    ok, reason = _mechanic_verdict(
        _v({"has_hook": True, "ran": True, "mechanic_matters": False}),
        declares_mechanic=True)
    assert ok is False and "no effect" in reason.lower()


def test_verdict_mechanic_matters_ok() -> None:
    ok, _ = _mechanic_verdict(
        _v({"has_hook": True, "ran": True, "mechanic_matters": True}),
        declares_mechanic=True)
    assert ok is True


# --------------------------------------------------------------------------- #
# fold into the recorded result + PR fields
# --------------------------------------------------------------------------- #
def _verdict(mp, ok=True, non_black=True, interaction_changed=True):
    return {"ok": ok, "non_black": non_black, "console_errors": [],
            "interaction_changed": interaction_changed, "reason": "rendered",
            "mechanic_probe": mp}


def test_result_blocks_declared_inert_game() -> None:
    # SPEC-40 (item B) MOVED this block one layer up. The mechanic verdict no longer
    # folds into the ANCHORED `web:probe` passed, because `anchors.reconcile` keys on
    # that boolean and a marginal differential was driving `anchor_regressed` ->
    # `revise_livelock`. The golf-2 protection this test guards now lives in
    # `completion.mechanic_gate_status` (item E path 3), locked by
    # test_spec40_white_box_oracle.py::test_gate_hierarchy_paths.
    #
    # What is still asserted HERE is the escape hatch: `mechanic_advisory=False` must
    # reproduce today's fold exactly.
    v = _verdict({"has_hook": True, "ran": True, "mechanic_matters": False})
    assert _verdict_to_result(
        v, declares_mechanic=True, mechanic_advisory=False).passed is False
    # SAME verdict passes when the project did not declare the claim.
    assert _verdict_to_result(
        v, declares_mechanic=False, mechanic_advisory=False).passed is True
    # ...and under the SPEC-40 default the differential is advisory, so it passes.
    assert _verdict_to_result(v, declares_mechanic=True).passed is True


def test_result_blocks_declared_no_hook_game() -> None:
    # See the note above — the block moved to the done-gate; the hook contract is
    # still named in the reason so the council can act on it either way.
    r = _verdict_to_result(_verdict({"has_hook": False}), declares_mechanic=True,
                           mechanic_advisory=False)
    assert r.passed is False and "window.__probe" in r.stderr_preview
    advisory = _verdict_to_result(_verdict({"has_hook": False}),
                                  declares_mechanic=True)
    assert advisory.passed is True
    assert "window.__probe" in advisory.stderr_preview


def test_result_passes_declared_live_game() -> None:
    v = _verdict({"has_hook": True, "ran": True, "mechanic_matters": True})
    assert _verdict_to_result(v, declares_mechanic=True).passed is True


def test_result_unchanged_for_non_declared_projects() -> None:
    v = _verdict({"has_hook": False})
    assert _verdict_to_result(v, declares_mechanic=False).passed is True


def test_result_advisory_when_phase_absent() -> None:
    # regression: a probe error (no mechanic_probe key) is not folded to a red.
    v = {"ok": True, "non_black": True, "console_errors": [],
         "interaction_changed": True, "reason": "rendered"}
    assert _verdict_to_result(v, declares_mechanic=True).passed is True


def test_pr_fields_fold_mechanic() -> None:
    v = _verdict({"has_hook": True, "ran": True, "mechanic_matters": False})
    f = _probe_verdict_fields(v, head="h1", declares_mechanic=True)
    assert f["probe_passed"] is False and f["probe_mechanic_ok"] is False
    assert f["probe_mechanic_has_hook"] is True


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
    # Robust parse: the verdict is the last stdout line that is JSON (mirrors
    # web_probe._default_node_runner, which scans in reverse for a '{' line).
    for line in reversed(out.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON verdict; stdout={out.stdout!r} stderr={out.stderr[-400:]!r}")


# (fixture, expect has_hook, expect ran, expect mechanic_matters, should_pass)
_LIVE_CASES = [
    ("live", True, True, True, True),       # gravity changes the outcome -> PASS
    ("inert", True, True, False, False),    # on == off -> inert -> BLOCK
    ("nohook", False, False, None, False),  # no hook -> BLOCK (declared)
    ("stub", True, False, None, False),     # hook present but state lacks ball/hole
    ("nondet", True, False, None, False),   # two identical shots diverge -> unusable
]


@pytest.mark.live
@pytest.mark.parametrize("fixture,has_hook,ran,matters,should_pass", _LIVE_CASES)
def test_live_mechanic_probe(fixture, has_hook, ran, matters, should_pass) -> None:
    if not shutil.which("node"):
        pytest.skip("node not available")
    httpd, port = _serve(_FIX / fixture)
    try:
        v = _probe(f"http://127.0.0.1:{port}/index.html")
    finally:
        httpd.shutdown()
    mp = v.get("mechanic_probe") or {}
    assert mp.get("has_hook") is has_hook, v
    assert mp.get("ran") is ran, v
    if ran:
        assert mp.get("mechanic_matters") is matters, v
    # end-to-end fold: a declared-mechanic project blocks all but the live game.
    r = _verdict_to_result(v, declares_mechanic=True)
    assert r.passed is should_pass, (fixture, r.stderr_preview)
