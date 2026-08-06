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
# Task 4 (item B) — the differential is advisory, not anchored
# --------------------------------------------------------------------------- #
def _v(**kw) -> dict:
    base = {"ok": True, "non_black": True, "console_errors": [],
            "interaction_changed": True, "reason": "rendered"}
    base.update(kw)
    return base


def test_marginal_differential_no_longer_reds_the_anchor() -> None:
    """Item B — the core of regression lock 4.

    ``anchors.reconcile`` keys on this exact boolean, so folding the mechanic verdict
    into it meant a marginal differential flipping on a sub-threshold tweak set
    ``anchor_regressed``, which fed ``revise_livelock`` and actively PUNISHED the
    tuning that flipped it (golf-4, decisions #197/#231).
    """
    from errorta_council.coding.web_probe import _verdict_to_result
    v = _v(mechanic_probe={"has_hook": True, "ran": True,
                           "mechanic_matters": False, "confident": False})
    assert _verdict_to_result(v, declares_mechanic=True).passed is True
    # ...and the escape hatch restores today's fold exactly.
    assert _verdict_to_result(
        v, declares_mechanic=True, mechanic_advisory=False).passed is False


def test_liveness_failure_still_reds() -> None:
    """The other half of lock 4: a REAL regression still breaks the lineage."""
    from errorta_council.coding.web_probe import _verdict_to_result
    assert _verdict_to_result(_v(non_black=False),
                              declares_mechanic=True).passed is False
    assert _verdict_to_result(_v(interaction_changed=False),
                              declares_mechanic=True).passed is False
    assert _verdict_to_result(_v(ok=False, console_errors=["boom"]),
                              declares_mechanic=True).passed is False


def test_whitebox_verdict_classification() -> None:
    from errorta_council.coding.web_probe import _whitebox_verdict
    # No `whitebox` key at all (an older probe script) -> absent, never a red.
    assert _whitebox_verdict(_v())[0] == "absent"
    assert _whitebox_verdict(_v(whitebox={"has_contract": False}))[0] == "absent"
    assert _whitebox_verdict(_v(whitebox={
        "has_contract": True, "ran": True, "verdict": "green"}))[0] == "green"
    status, reason = _whitebox_verdict(_v(whitebox={
        "has_contract": True, "ran": True, "verdict": "red",
        "reason": "vacuous — ... setMechanic(false) ..."}))
    assert status == "red"
    assert "setMechanic" in reason
    # A contract that could not RUN is not a red — it falls through to path 3/4.
    assert _whitebox_verdict(_v(whitebox={
        "has_contract": True, "ran": False,
        "reason": "won() threw"}))[0] == "absent"


# --------------------------------------------------------------------------- #
# Task 5 (item C) — the reviewed signal equals the gating signal
# --------------------------------------------------------------------------- #
def test_per_pr_arm_stamps_the_same_components() -> None:
    """Regression lock 7 — the feedback-locality fix.

    22 green PRs merged against a weaker per-PR verdict while the master differential
    that gates delivery stayed red. Both arms must now stamp the same components.
    """
    from errorta_council.coding.web_probe import _probe_verdict_fields
    v = _v(mechanic_probe={"has_hook": True, "ran": True,
                           "mechanic_matters": False, "confident": True},
           whitebox={"has_contract": True, "ran": True, "verdict": "red",
                     "reason": "vacuous — setMechanic(false) does not disable it"})
    for declares in (True, False):
        f = _probe_verdict_fields(v, head="abc", declares_mechanic=declares)
        assert f["probe_whitebox"] == "red", f
        assert "setMechanic" in f["probe_whitebox_reason"], f
        assert f["probe_mechanic_confident"] is True, f
        assert f["probe_mechanic_matters"] is False, f


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


# --------------------------------------------------------------------------- #
# Task 2 (item A) — the adaptive sweep, live
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_live_golf4_adaptive_sweep_finds_the_band() -> None:
    """Regression lock 2 — golf-4 becomes winnable.

    The golf-4 false red: the geometry-anchored ``[0.8,1.3,2.0] x D`` sweep fires at
    480/780/1200, where ``shoot()`` (a SPEED) launches at 28,800 px/tick and the ball
    crosses the 600px course in ~one tick, so ON is identical to OFF. Measured on this
    fixture the endpoint gap at those powers is 1-5px; at every power <= 15 it is
    755-3688px. The adaptive sweep must bisect into the game's own scale and sample
    the live band.
    """
    mp = _probe_fixture("spec40", "golf4")["mechanic_probe"]
    assert mp["ran"] is True, mp
    assert mp["mechanic_matters"] is True, mp
    assert mp["p_sink"] is not None and mp["p_sink"] < 100, mp
    assert max(mp["powers"]) < 100, mp


@pytest.mark.live
def test_live_truly_inert_is_false_and_confident() -> None:
    """Regression lock 1 — the golf-2 protection survives the recalibration.

    A game whose ``step()`` never reads ``mechanicOn`` (the shipped-inert defect
    golf-2 actually had) must report ``mechanic_matters`` False and CONFIDENTLY so:
    path 3 can only hard-block on a confident verdict, so an inert game that came back
    "uncertain" would ship — exactly the golf-2 failure.
    """
    mp = _probe_fixture("spec40", "inert-true")["mechanic_probe"]
    assert mp["ran"] is True, mp
    assert mp["mechanic_matters"] is False, mp
    assert mp["confident"] is True, mp
    assert mp["max_gap"] <= 1, mp


@pytest.mark.live
def test_live_weak_but_real_gravity_is_uncertain_not_inert() -> None:
    """The grey band, doing its job — and why it is NOT tuned away.

    ``fixtures/spec37/inert`` is misleadingly named: ``GSCALE=8`` is weak but REAL
    gravity, measurably deflecting the ball ~14px against a 20px hole radius. Under
    SPEC-37's ``gap > holeR`` rule that reads as a flat "no effect" — a false red of
    exactly golf-4's kind, just smaller. SPEC-40 classifies it as UNCERTAIN, so it is
    advisory and can never hard-block or drive an anchor regression.

    Loosening the ``holeR/2`` grey band to make this "confidently inert" would re-open
    the golf-4 false red, so the band stays and the honest verdict is uncertainty.
    """
    mp = _probe_fixture("spec37", "inert")["mechanic_probe"]
    assert mp["ran"] is True, mp
    assert mp["confident"] is False, mp


# --------------------------------------------------------------------------- #
# Task 3 (item D) — the white-box phase, live
# --------------------------------------------------------------------------- #
@pytest.mark.live
@pytest.mark.parametrize("fixture,verdict,needle", [
    ("whitebox-green", "green", "wins with the mechanic on"),
    ("whitebox-vacuous", "red", "setMechanic"),
    ("whitebox-red", "red", "does not win"),
])
def test_live_whitebox_arms(fixture: str, verdict: str, needle: str) -> None:
    """Item D's three arms, one fixture per outcome.

    ``whitebox-vacuous`` is golf-4's literal defect — ``setMechanic`` is a no-op, so
    the engine's negative control cannot disable the mechanic and the solution wins in
    BOTH arms. The reason must name ``setMechanic``: that is the actionable message the
    opaque "the mechanic has NO effect" never gave the council.
    """
    wb = _probe_fixture("spec40", fixture)["whitebox"]
    assert wb["has_contract"] is True, wb
    assert wb["ran"] is True, wb
    assert wb["verdict"] == verdict, wb
    assert needle in wb["reason"], wb


@pytest.mark.live
def test_live_no_contract_is_not_a_red() -> None:
    """A contract-less game falls through to path 3/4 — never a white-box red.

    The new verbs are deliberately NOT mandatory (that would re-create the very
    instrumentation burden this spec exists to retire), so their absence must be
    reported as "no contract", not as a failure.
    """
    wb = _probe_fixture("spec37", "live")["whitebox"]
    assert wb["has_contract"] is False, wb
    assert wb["verdict"] is None, wb


@pytest.mark.live
def test_live_nondeterministic_game_is_not_confident() -> None:
    """Regression lock 4 — a nondeterministic game routes to UNCERTAIN, not red.

    The determinism guard already refuses to attribute an on/off difference to the
    mechanic. Under SPEC-40 that must surface as ``ran: False`` (advisory), never as a
    confident inert verdict that could hard-block.
    """
    mp = _probe_fixture("spec37", "nondet")["mechanic_probe"]
    assert mp["ran"] is False, mp
    assert "non-deterministic" in mp["reason"], mp
