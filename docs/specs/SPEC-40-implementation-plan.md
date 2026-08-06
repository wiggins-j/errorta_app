# SPEC-40 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the testability-contract oracle sound, so the council can converge — a
white-box `solution()`/`won()` acceptance path that is the primary delivery verdict, a
recalibrated differential demoted to advisory, and a done-gate hierarchy that keeps golf-2
blocked while unblocking golf-4.

**Architecture:** Four phases already run in `scripts/web-probe.mjs` (liveness →
interaction → differential); this adds a fourth (white-box) and recalibrates the third.
`web_probe.py` stops folding the mechanic verdict into the anchored `web:probe` `passed`
and instead persists structured evidence to `run_state`, which a new
`completion.mechanic_gate_status` classifies into the four-path hierarchy consumed at the
`done` chokepoint in `runner.py`.

**Tech Stack:** Python 3.11+ (`errorta_council.coding`), Node ESM + Playwright/Chromium
(`scripts/web-probe.mjs`), pytest (`python/tests/coding/`).

## Global Constraints

- **Escape hatches.** Four knobs on `CodingAutonomyPolicy`, each disable-value reproducing
  today's trace exactly: `probe_adaptive_sweep`, `probe_mechanic_advisory`,
  `probe_whitebox`, `probe_pr_gating`. All default `True`.
- **SPEC-39 invariant (do not break).** The differential and the white-box arms must each
  stay within ONE synchronous `page.evaluate` — a sync evaluate freezes the page's rAF /
  timers so `tick()` is the sole driver. Splitting one, or `await`ing mid-sweep, silently
  reintroduces the determinism bug SPEC-39's pause clause used to paper over.
- **Fail-open everywhere.** `web_probe.py` never raises into the loop and never invents a
  block: any spawn/parse/timeout failure degrades to *no evidence*. Every new uncertainty
  routes to advisory, never to red.
- **No `runner` import** in `web_probe.py` / `completion.py` / `anchors.py` / `gate_state.py`
  (circular — `runner` imports `.topology` / `.schemas` at import time).
- **Tick budget:** 400,000 ticks across the whole differential phase; the existing 20s
  timebox stands. Exhausting either → `confident: false`, never red.
- **`power` stays the game's own launch-speed unit.** No `[0,1]` migration, no fixture or
  DoD-template churn.
- **Live tests are opt-in** (`pytest -m live`); the default `addopts` excludes them. Every
  behavior must ALSO be locked by a unit test over a synthetic verdict dict.

---

### Task 1: Policy knobs

**Files:**
- Modify: `python/errorta_council/coding/autonomy.py` (`CodingAutonomyPolicy`,
  `policy_to_dict`, `policy_from_dict`)
- Test: `python/tests/coding/test_spec40_white_box_oracle.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `CodingAutonomyPolicy.probe_adaptive_sweep: bool`,
  `.probe_mechanic_advisory: bool`, `.probe_whitebox: bool`, `.probe_pr_gating: bool`;
  all round-trip through `policy_to_dict` / `policy_from_dict`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && pytest tests/coding/test_spec40_white_box_oracle.py -v`
Expected: FAIL with `AttributeError: 'CodingAutonomyPolicy' object has no attribute 'probe_adaptive_sweep'`

- [ ] **Step 3: Write minimal implementation**

Add to `CodingAutonomyPolicy`, following the batch comment convention already used for
`last_word_limit` / `narrow_limit` (state what the disable value restores):

```python
    # SPEC-40 (item A): use the ADAPTIVE power sweep (bisect for the minimum power at
    # which a mechanic-OFF straight shot sinks, then sweep the band below it) instead
    # of the geometry-anchored [0.8,1.3,2.0]xD sweep, which is 32-80x miscalibrated
    # against a game whose shoot() takes a speed. False restores the old sweep exactly.
    probe_adaptive_sweep: bool = True
    # SPEC-40 (item B): keep the mechanic differential OUT of the anchored web:probe
    # `passed`, so a marginal verdict can no longer drive anchor_regressed /
    # revise_livelock. False restores folding mechanic_ok into `passed`.
    probe_mechanic_advisory: bool = True
    # SPEC-40 (item D): run the white-box solution()/won() phase. False skips it
    # entirely — no new page evaluation, today's emitted JSON.
    probe_whitebox: bool = True
    # SPEC-40 (item C): stamp the same verdict components on the PER-PR arm that gate
    # delivery, so the reviewer reviews against the real bar. False restores today's
    # weaker per-PR verdict.
    probe_pr_gating: bool = True
```

Then add the four keys to `policy_to_dict` (mirroring the existing `"narrow_limit": p.narrow_limit` lines) and to `policy_from_dict` as `bool(d.get(<key>, base.<key>))`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && pytest tests/coding/test_spec40_white_box_oracle.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/errorta_council/coding/autonomy.py python/tests/coding/test_spec40_white_box_oracle.py
git commit -m "feat(coding): SPEC-40 — policy knobs for the oracle rework"
```

---

### Task 2: Adaptive power sweep (move A)

**Files:**
- Modify: `scripts/web-probe.mjs` (the `mechanic_probe` phase, currently lines ~250–346)
- Create: `python/tests/coding/fixtures/spec40/golf4/index.html`
- Test: `python/tests/coding/test_spec40_white_box_oracle.py`

**Interfaces:**
- Consumes: nothing from Task 1 (the mjs script is engine-side; the knob gates the Python
  caller, which passes `--legacy-sweep` when `probe_adaptive_sweep` is False).
- Produces: the emitted `mechanic_probe` object gains
  `{confident: bool, p_sink: number|null, powers: number[], max_gap: number}` alongside
  today's `{has_hook, ran, wells, mechanic_matters, reason}`.

**The `golf4` fixture must replicate golf-4's actual defect:** `POWER_SCALE = 60`, a human
drag capped at `MAX_POWER = 15`, and a `__probe.shoot` that does **not** clamp. Gravity must
matter in the 3–8 power band and not at 480+.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.live
def test_live_golf4_adaptive_sweep_finds_the_band() -> None:
    # The golf-4 false red: the old [0.8,1.3,2.0]xD sweep fires at 480-1200 where the
    # ball crosses the course in one tick, so ON == OFF. The adaptive sweep must find
    # P_sink in the game's own scale and sample the 3-8 band where gravity bends it.
    httpd, port = _serve(_FIX / "spec40" / "golf4")
    try:
        v = _probe(f"http://127.0.0.1:{port}/")
    finally:
        httpd.shutdown()
    mp = v["mechanic_probe"]
    assert mp["ran"] is True, mp
    assert mp["mechanic_matters"] is True, mp
    assert mp["p_sink"] is not None and mp["p_sink"] < 100, mp
    assert max(mp["powers"]) < 100, mp


@pytest.mark.live
def test_live_inert_stays_false_and_confident() -> None:
    # Regression lock 1 (the golf-2 protection): a genuinely inert game must still
    # report mechanic_matters False, and CONFIDENTLY so, or path 3 cannot block it.
    httpd, port = _serve(_FIX / "spec37" / "inert")
    try:
        v = _probe(f"http://127.0.0.1:{port}/")
    finally:
        httpd.shutdown()
    mp = v["mechanic_probe"]
    assert mp["ran"] is True, mp
    assert mp["mechanic_matters"] is False, mp
    assert mp["confident"] is True, mp
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && pytest tests/coding/test_spec40_white_box_oracle.py -v -m live`
Expected: FAIL — `KeyError: 'p_sink'` (the field does not exist yet).

- [ ] **Step 3: Implement the sweep inside the existing synchronous `page.evaluate`**

Replace `const powers = [0.8, 1.3, 2.0].map((k) => k * D);` and the loop below it. Keep
`runShot` exactly as-is (it is already hardened and geometric). Add before the loop:

```js
          // SPEC-40 (item A): calibrate to the GAME's usable power range instead of
          // the hole geometry. The OFF arm is MONOTONIC in power (no mechanic => a
          // straight line whose reach grows with launch speed), so "does it sink"
          // flips exactly once and a bisect is sound. The ON arm is not monotonic —
          // that is the mechanic's whole point — which is why we bracket on OFF.
          let ticks = 0;
          const TICK_BUDGET = 400000;
          const sinksOff = (p) => {
            const r = runShot(p, false);
            ticks += (r && r.ticks) || 0;
            return r === null ? null : r.sank;
          };
          let lo = 0, hi = 0, bracketed = false;
          for (let p = 1, i = 0; i < 14 && ticks < TICK_BUDGET; i++, p *= 2) {
            const s = sinksOff(p);
            if (s === null) break;
            if (s) { hi = p; bracketed = true; break; }
            lo = p;
          }
          let pSink = null;
          if (bracketed) {
            for (let i = 0; i < 12 && ticks < TICK_BUDGET; i++) {
              const mid = (lo + hi) / 2;
              const s = sinksOff(mid);
              if (s === null) break;
              if (s) hi = mid; else lo = mid;
              if ((hi - lo) / hi < 0.01) break;
            }
            pSink = hi;
          }
          // Sweep the band at and BELOW P_sink: that is where the ball is slow enough
          // for the mechanic to have integration time to bend it.
          const powers = pSink
            ? Array.from({length: 7}, (_, i) =>
                pSink * Math.pow(1.5 / 0.15, i / 6) * 0.15)
            : [0.8, 1.3, 2.0].map((k) => k * D);   // no bracket -> old sweep, uncertain
```

Then in the comparison loop, accumulate the confidence evidence instead of breaking early
on the first difference:

```js
          let matters = false, anyMoved = false, resetOk = true, nondet = false;
          let completed = 0, maxGap = 0, greyBand = false;
          for (const pow of powers) {
            if (ticks > TICK_BUDGET) break;
            const on = runShot(pow, true);
            const off = runShot(pow, false);
            const off2 = runShot(pow, false);
            if (on === null || off === null || off2 === null) { resetOk = false; break; }
            ticks += (on.ticks || 0) + (off.ticks || 0) + (off2.ticks || 0);
            if (on.maxMove > 2 || off.maxMove > 2) anyMoved = true;
            if (off.sank !== off2.sank || dist(off.end, off2.end) > holeR / 2) {
              nondet = true; break;
            }
            completed++;
            const gap = dist(on.end, off.end);
            maxGap = Math.max(maxGap, gap);
            if (on.sank !== off.sank || gap > holeR) matters = true;
            else if (gap > holeR / 2) greyBand = true;   // near-threshold -> uncertain
          }
          // SPEC-40 item B: CONFIDENT requires a converged bracket, >=5 completed
          // powers, no grey-band gap, and no observed nondeterminism. Anything else is
          // advisory — the failure mode is a MISSED block, never a false red.
          const confident = bracketed && completed >= 5 && !greyBand
                            && !nondet && ticks <= TICK_BUDGET;
```

And extend the returned object with `p_sink: pSink, confident, max_gap: Math.round(maxGap)`.

`runShot` must also return its tick count — change `return { sank, end, maxMove };` to
`return { sank, end, maxMove, ticks: i };` and hoist `let i` out of the `for` header so it
is readable after the loop.

- [ ] **Step 4: Write the golf4 fixture**

Create `python/tests/coding/fixtures/spec40/golf4/index.html` modeled on
`fixtures/spec37/live/index.html`, with golf-4's scale:

```html
<!doctype html><html><head><meta charset="utf-8"><title>golf4</title>
<style>html,body{margin:0;background:#0b1020}</style></head>
<body><canvas id="c" width="800" height="600"></canvas>
<script type="module">
const cv=document.getElementById("c"), g=cv.getContext("2d");
// golf-4's actual scale: the human drag clamps to MAX_POWER, __probe.shoot does NOT.
const DT=1/60, FRICTION=0.99, MIN=10, GSCALE=9000, BR=10, POWER_SCALE=60, MAX_POWER=15;
const tee={x:100,y:300}, hole={x:700,y:300,r:20};
const wells=[{x:400,y:180,strength:900,r:40}];
let ball={x:tee.x,y:tee.y,vx:0,vy:0,r:BR}, sank=false, mechanicOn=true;
function reset(){ball={x:tee.x,y:tee.y,vx:0,vy:0,r:BR}; sank=false;}
function step(){
  if(sank) return;
  if(mechanicOn){for(const w of wells){const dx=w.x-ball.x,dy=w.y-ball.y,d=Math.hypot(dx,dy),ed=Math.max(d,MIN);
    const a=GSCALE*w.strength/(ed*ed); if(d>0){ball.vx+=dx/d*a*DT; ball.vy+=dy/d*a*DT;}}}
  ball.vx*=FRICTION; ball.vy*=FRICTION; ball.x+=ball.vx*DT; ball.y+=ball.vy*DT;
  if(Math.hypot(ball.x-hole.x,ball.y-hole.y)<=hole.r) sank=true;
}
cv.addEventListener("mousedown",()=>{ball.vx=Math.min(MAX_POWER,10)*POWER_SCALE;ball.vy=0;});
function draw(){g.fillStyle="#0b1020";g.fillRect(0,0,800,600);
  g.fillStyle="#4af";for(const w of wells){g.beginPath();g.arc(w.x,w.y,w.r,0,7);g.fill();}
  g.strokeStyle="#5f5";g.lineWidth=3;g.beginPath();g.arc(hole.x,hole.y,hole.r,0,7);g.stroke();
  g.fillStyle="#fff";g.beginPath();g.arc(ball.x,ball.y,ball.r,0,7);g.fill();}
function loop(){step();draw();requestAnimationFrame(loop);} loop();
window.__probe={
  state:()=>({ball:{x:ball.x,y:ball.y},hole:{...hole},wells:wells.map(w=>({x:w.x,y:w.y})),moving:Math.hypot(ball.vx,ball.vy)>0.5}),
  shoot:(dx,dy,power)=>{const d=Math.hypot(dx,dy)||1;ball.vx=dx/d*power*POWER_SCALE;ball.vy=dy/d*power*POWER_SCALE;},
  tick:(n)=>{for(let i=0;i<n;i++)step();}, reset, setMechanic:(on)=>{mechanicOn=!!on;},
};
</script></body></html>
```

**Calibrate the fixture before moving on:** run the live test and confirm gravity actually
matters in the low band and not the high one. If `mechanic_matters` is already true at the
old `[0.8,1.3,2.0]×D` powers, the fixture is not reproducing golf-4 — tune `GSCALE` /
`strength` / well position until the OLD sweep says false and the NEW sweep says true. That
contrast IS regression lock 2; a fixture that passes both sweeps proves nothing.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd python && pytest tests/coding/test_spec40_white_box_oracle.py -v -m live`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/web-probe.mjs python/tests/coding/fixtures/spec40/golf4/ python/tests/coding/test_spec40_white_box_oracle.py
git commit -m "feat(coding): SPEC-40 (A) — adaptive power sweep calibrated to the game"
```

---

### Task 3: White-box phase (move D)

**Files:**
- Modify: `scripts/web-probe.mjs` (new phase after the differential)
- Create: `python/tests/coding/fixtures/spec40/{whitebox-green,whitebox-vacuous,whitebox-red}/index.html`
- Test: `python/tests/coding/test_spec40_white_box_oracle.py`

**Interfaces:**
- Consumes: Task 2's `runShot` helper and calibrated `powers` (arm 3 reuses the sweep,
  anchored to `solution().power` when present).
- Produces: a new top-level `whitebox` key in the emitted JSON:
  `{has_contract: bool, ran: bool, solved_on: bool|null, solved_off: bool|null,
    straight_wins: bool|null, verdict: "green"|"red"|null, reason: string}`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.live
@pytest.mark.parametrize("fixture,verdict,needle", [
    ("whitebox-green", "green", ""),
    ("whitebox-vacuous", "red", "setMechanic"),
    ("whitebox-red", "red", "does not win"),
])
def test_live_whitebox_arms(fixture, verdict, needle) -> None:
    httpd, port = _serve(_FIX / "spec40" / fixture)
    try:
        v = _probe(f"http://127.0.0.1:{port}/")
    finally:
        httpd.shutdown()
    wb = v["whitebox"]
    assert wb["has_contract"] is True, wb
    assert wb["ran"] is True, wb
    assert wb["verdict"] == verdict, wb
    assert needle in wb["reason"], wb


@pytest.mark.live
def test_live_no_contract_is_not_a_red() -> None:
    # A game without solution()/won() falls through to item E path 3/4 — the white-box
    # phase must report "no contract", never a red verdict.
    httpd, port = _serve(_FIX / "spec37" / "live")
    try:
        v = _probe(f"http://127.0.0.1:{port}/")
    finally:
        httpd.shutdown()
    assert v["whitebox"]["has_contract"] is False, v["whitebox"]
    assert v["whitebox"]["verdict"] is None, v["whitebox"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && pytest tests/coding/test_spec40_white_box_oracle.py -v -m live -k whitebox`
Expected: FAIL — `KeyError: 'whitebox'`.

- [ ] **Step 3: Implement the phase**

Add after the `mechanic_probe` block, inside the same `try`. It MUST be one synchronous
`page.evaluate` (the SPEC-39 invariant) and MUST snapshot `solution()` before either arm:

```js
    // SPEC-40 (item D) — the WHITE-BOX phase, and the primary delivery verdict.
    // Two additive hook fields the game already knows: won() is the predicate it uses
    // to draw its own win banner; solution() is a shot that clears the level, in the
    // SAME units shoot() takes. The engine drives all three arms, so no council-authored
    // code is evaluated — we call two functions and read a boolean and three numbers.
    let whitebox = { has_contract: false, ran: false, solved_on: null,
                     solved_off: null, straight_wins: null, verdict: null, reason: "" };
    try {
      const r = await page.evaluate(() => {
        const P = window.__probe;
        const ok = (f) => P && typeof P[f] === "function";
        if (!(ok("won") && ok("solution") && ok("shoot") && ok("tick")
              && ok("reset") && ok("setMechanic"))) {
          return { has_contract: false, ran: false, reason: "no solution()/won() contract" };
        }
        // Snapshot the solution ONCE, as primitives, BEFORE either arm — so it cannot
        // observe which arm is running, return a different shot per arm, or alias live
        // game state.
        P.reset();
        let s;
        try { s = P.solution(); } catch (e) { s = null; }
        if (!s || typeof s.dx !== "number" || typeof s.dy !== "number"
            || typeof s.power !== "number") {
          return { has_contract: true, ran: false,
                   reason: "solution() did not return {dx,dy,power} numbers" };
        }
        const shot = { dx: s.dx, dy: s.dy, power: s.power };
        const runArm = (on) => {
          P.reset();
          P.setMechanic(on);
          P.shoot(shot.dx, shot.dy, shot.power);
          for (let i = 0; i < 6000; i++) {
            P.tick(1);
            if (P.won()) return true;
          }
          return false;
        };
        const solvedOn = runArm(true);
        const solvedOff = runArm(false);
        P.setMechanic(true);   // restore
        return { has_contract: true, ran: true, solved_on: solvedOn,
                 solved_off: solvedOff };
      }).catch(() => ({ has_contract: false, ran: false, reason: "white-box phase threw" }));
      Object.assign(whitebox, r);
    } catch { /* best-effort; an absent phase falls through to path 3/4 */ }
```

Then compose the verdict in Node (not in-page), folding arm 3 from the differential's
straight-shot sweep:

```js
    if (whitebox.ran) {
      if (!whitebox.solved_on) {
        whitebox.verdict = "red";
        whitebox.reason = "your own solution() does not win with the mechanic on — "
          + "either solution() is wrong or the mechanic does not do what the level needs";
      } else if (whitebox.solved_off) {
        whitebox.verdict = "red";
        whitebox.reason = "vacuous — the solution wins with the mechanic DISABLED; "
          + "either this level is solvable without the mechanic, or setMechanic(false) "
          + "does not actually disable it";
      } else {
        whitebox.verdict = "green";
        whitebox.reason = "solution() wins with the mechanic on and fails with it off";
      }
    }
```

**Arm 3** (the straight-shot-must-fail claim) reuses the differential's sweep rather than
re-running shots. Note it is a *distinct* claim from `mechanic_matters` — a mechanic can
matter while a straight shot still sinks — so do NOT derive one from the other. Instead:

1. In Task 2's differential loop, track `straightSank = straightSank || on.sank` (any swept
   **ON** shot that sinks is a straight-line solution) and return it in the phase object.
2. Copy it across: `whitebox.straight_wins = mechanicProbe.ran ? mechanicProbe.straight_sank : null`.
3. After composing the verdict above, downgrade a green: if `whitebox.verdict === "green"`
   and `whitebox.straight_wins === true`, set `verdict = "red"` with the reason
   `"a straight shot at the hole sinks — the DoD forbids straight-line solutions"`.

Finally add `whitebox` to the `emit({...})` call.

- [ ] **Step 4: Write the three fixtures**

All three are copies of the `golf4` fixture with `won` and `solution` added and one
property varied. `won: () => sank`, and:

- `whitebox-green`: `solution: () => ({dx: 1, dy: -0.55, power: 7})` — an arcing shot that
  needs the well to curve into the hole. **Calibrate it**: the shot must sink with the
  mechanic on and miss with it off. Add a straight wall or move the hole off-axis so a
  straight shot cannot reach it.
- `whitebox-vacuous`: same, but `setMechanic` is a no-op (`setMechanic: () => {}`) — the
  literal golf-4 defect. The solution then wins in both arms.
- `whitebox-red`: same as green but `solution: () => ({dx: 1, dy: 0, power: 1})` — a shot
  that does not reach the hole under any mechanic state.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd python && pytest tests/coding/test_spec40_white_box_oracle.py -v -m live`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/web-probe.mjs python/tests/coding/fixtures/spec40/ python/tests/coding/test_spec40_white_box_oracle.py
git commit -m "feat(coding): SPEC-40 (D) — white-box solution()/won() acceptance phase"
```

---

### Task 4: Fold the verdicts in `web_probe.py` (moves B + E, engine half)

**Files:**
- Modify: `python/errorta_council/coding/web_probe.py` (`_HOOK_CONTRACT`,
  `_verdict_to_result`, `_probe_verdict_fields`, new `_whitebox_verdict`,
  new `record_mechanic_evidence`)
- Test: `python/tests/coding/test_spec40_white_box_oracle.py`

**Interfaces:**
- Consumes: Task 3's emitted `whitebox` object and Task 2's `mechanic_probe.confident`.
- Produces: `web_probe._whitebox_verdict(verdict) -> tuple[str, str]` returning
  `("green"|"red"|"absent", reason)`; `web_probe.record_mechanic_evidence(store, head,
  verdict) -> None` persisting `run_state["probe_mechanic_evidence"] =
  {"head": str, "whitebox": str, "whitebox_reason": str, "mechanic_matters": bool|None,
   "confident": bool, "reason": str}`.

- [ ] **Step 1: Write the failing tests (unit — no browser)**

```python
def _v(**kw):
    base = {"ok": True, "non_black": True, "console_errors": [],
            "interaction_changed": True, "reason": "rendered"}
    base.update(kw)
    return base


def test_marginal_differential_no_longer_reds_the_anchor() -> None:
    # Item B: the anchored web:probe `passed` tracks liveness only, so a red mechanic
    # differential can no longer drive anchor_regressed / revise_livelock.
    from errorta_council.coding.web_probe import _verdict_to_result
    v = _v(mechanic_probe={"has_hook": True, "ran": True,
                           "mechanic_matters": False, "confident": False})
    assert _verdict_to_result(v, declares_mechanic=True).passed is True


def test_liveness_failure_still_reds() -> None:
    # ...but a REAL liveness break still breaks the lineage (regression lock 4).
    from errorta_council.coding.web_probe import _verdict_to_result
    assert _verdict_to_result(_v(non_black=False), declares_mechanic=True).passed is False
    assert _verdict_to_result(_v(interaction_changed=False),
                              declares_mechanic=True).passed is False


def test_whitebox_verdict_classification() -> None:
    from errorta_council.coding.web_probe import _whitebox_verdict
    assert _whitebox_verdict(_v(whitebox={"has_contract": False}))[0] == "absent"
    assert _whitebox_verdict(
        _v(whitebox={"has_contract": True, "ran": True, "verdict": "green"}))[0] == "green"
    status, reason = _whitebox_verdict(_v(whitebox={
        "has_contract": True, "ran": True, "verdict": "red",
        "reason": "vacuous — ... setMechanic(false) ..."}))
    assert status == "red" and "setMechanic" in reason
    # A phase that could not run is NOT a red — it falls through to path 3/4.
    assert _whitebox_verdict(
        _v(whitebox={"has_contract": True, "ran": False}))[0] == "absent"
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && pytest tests/coding/test_spec40_white_box_oracle.py -v`
Expected: FAIL — `ImportError: cannot import name '_whitebox_verdict'`, and
`test_marginal_differential_no_longer_reds_the_anchor` fails because `passed` is still
folding `mechanic_ok`.

- [ ] **Step 3: Implement**

1. In `_verdict_to_result`, drop `mechanic_ok` from the `passed` conjunction (keep
   computing it and keep appending `mechanic_reason` to the detail text, so
   `gate_state.latest_gate_text` still shows the reviewer the real line). Guard with the
   knob: when `probe_mechanic_advisory` is False, fold it exactly as today. Thread the knob
   in as a keyword argument with a `True` default so no call site changes signature-wise.
2. Add `_whitebox_verdict(verdict)` per the interface above: `absent` when the `whitebox`
   key is missing, `has_contract` is falsy, or `ran` is falsy; otherwise mirror
   `verdict["whitebox"]["verdict"]`.
3. Extend `_probe_verdict_fields` with `probe_whitebox` (the status string),
   `probe_whitebox_reason` (capped at 500), `probe_mechanic_confident`, and
   `probe_mechanic_matters`.
4. Add `record_mechanic_evidence(store, head, verdict)` writing the run_state key. Best
   effort, never raises (mirror `anchors.promote_anchor`).
5. Extend `_HOOK_CONTRACT` with the two new fields — worded as the *fast path*, not a
   mandate, since path 3 is still clearable on the differential alone:

```python
    "; OPTIONALLY also expose won:()=>bool (your own win predicate for the current "
    "level) and solution:()=>({dx,dy,power}) returning a shot that clears the level in "
    "the SAME units shoot() takes — that is the fastest, unambiguous way to prove the "
    "mechanic: the probe fires your solution with the mechanic on (must win) and the "
    "same shot with it off (must NOT win) (SPEC-40)"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && pytest tests/coding/test_spec40_white_box_oracle.py -v`
Expected: PASS

- [ ] **Step 5: Run the existing suites to catch regressions**

Run: `cd python && pytest tests/coding/test_spec37_behavioral_oracle.py tests/coding/test_spec38_interaction_gate.py tests/coding/test_gl01_anchors.py -v`
Expected: PASS. `test_spec37_*` asserts the OLD fold (`mechanic_matters False → passed
False`); those assertions must be updated to pass `probe_mechanic_advisory=False` or
re-pointed at the new done-gate, with a comment naming SPEC-40 as the reason. Do NOT delete
them — they are the golf-2 protection, now enforced one layer up.

- [ ] **Step 6: Commit**

```bash
git add python/errorta_council/coding/web_probe.py python/tests/coding/
git commit -m "feat(coding): SPEC-40 (B) — mechanic verdict is advisory, not anchored"
```

---

### Task 5: Per-PR arm parity (move C)

**Files:**
- Modify: `python/errorta_council/coding/web_probe.py` (`run_and_record`)
- Test: `python/tests/coding/test_spec40_white_box_oracle.py`

**Interfaces:**
- Consumes: Task 4's `_whitebox_verdict`, `_probe_verdict_fields`.
- Produces: no new symbols; `run_and_record`'s `pr_scoped=True` path now computes and
  stamps the same components as the master arm.

- [ ] **Step 1: Write the failing test**

```python
def test_per_pr_arm_stamps_the_same_components(tmp_path) -> None:
    # Item C — the feedback-locality bug: the reviewer saw a green per-PR verdict while
    # the master differential stayed red, so 22 green PRs merged and delivery never
    # cleared. Both arms must now surface the same components.
    from errorta_council.coding.web_probe import _probe_verdict_fields
    v = {"ok": True, "non_black": True, "console_errors": [],
         "interaction_changed": True, "reason": "rendered",
         "mechanic_probe": {"has_hook": True, "ran": True,
                            "mechanic_matters": False, "confident": True},
         "whitebox": {"has_contract": True, "ran": True, "verdict": "red",
                      "reason": "vacuous — setMechanic(false) does not disable it"}}
    for pr_scoped in (True, False):
        f = _probe_verdict_fields(v, head="abc", declares_mechanic=not pr_scoped)
        assert f["probe_whitebox"] == "red"
        assert "setMechanic" in f["probe_whitebox_reason"]
        assert f["probe_mechanic_confident"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && pytest tests/coding/test_spec40_white_box_oracle.py::test_per_pr_arm_stamps_the_same_components -v`
Expected: FAIL — the fields are computed but the per-PR arm short-circuits
`declares_mechanic` to False before they are derived.

- [ ] **Step 3: Implement**

In `run_and_record`, split the two concerns that `declares_mechanic` currently conflates:

```python
        # SPEC-40 (item C): COMPUTE the verdict components on both arms — the reviewer
        # must review against the same bar that gates delivery (the feedback-locality
        # bug: 22 green PRs merged against a weaker per-PR verdict while the master
        # differential stayed red). What stays arm-scoped is what HARD-GATES: a
        # partial-module PR mid-build legitimately has no whole-game hook yet, so the
        # hard component bites per-PR only once the contract is actually present.
        declares = _declares_load_bearing_mechanic(store)
        wb_status, _ = _whitebox_verdict(verdict)
        contract_present = wb_status != "absent"
        gates_hard = declares and (not pr_scoped or contract_present)
```

Use `gates_hard` where `declares_mechanic` was passed, and pass `declares` to
`_probe_verdict_fields` on both arms so the stamped record is always complete. Guard the
whole change behind `probe_pr_gating`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && pytest tests/coding/test_spec40_white_box_oracle.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/errorta_council/coding/web_probe.py python/tests/coding/test_spec40_white_box_oracle.py
git commit -m "feat(coding): SPEC-40 (C) — the reviewed signal equals the gating signal"
```

---

### Task 6: The done-gate hierarchy (move E)

**Files:**
- Modify: `python/errorta_council/coding/completion.py` (new `mechanic_gate_status`)
- Modify: `python/errorta_council/coding/runner.py` (`_acceptance_gate_blocks_done`, ~3495)
- Test: `python/tests/coding/test_spec40_white_box_oracle.py`

**Interfaces:**
- Consumes: Task 4's `run_state["probe_mechanic_evidence"]`.
- Produces: `completion.mechanic_gate_status(ledger, current_head) ->
  Literal["ok", "red", "advisory"]` and `completion.mechanic_gate_reason(ledger) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
class _FakeLedger:
    def __init__(self, ev): self._ev = ev
    def get_run_state(self): return {"probe_mechanic_evidence": self._ev}


def test_gate_hierarchy_paths() -> None:
    from errorta_council.coding.completion import mechanic_gate_status
    H = "deadbeefcafe"
    # path 1: contract green -> ok, and it OVERRIDES a red advisory differential
    assert mechanic_gate_status(_FakeLedger(
        {"head": H, "whitebox": "green", "mechanic_matters": False,
         "confident": True}), H) == "ok"
    # path 2: contract red -> red
    assert mechanic_gate_status(_FakeLedger(
        {"head": H, "whitebox": "red", "mechanic_matters": True,
         "confident": True}), H) == "red"
    # path 3: no contract + CONFIDENT inert -> red (the golf-2 protection)
    assert mechanic_gate_status(_FakeLedger(
        {"head": H, "whitebox": "absent", "mechanic_matters": False,
         "confident": True}), H) == "red"
    # path 4: no contract + uncertain -> advisory, NEVER a hard block (golf-4)
    assert mechanic_gate_status(_FakeLedger(
        {"head": H, "whitebox": "absent", "mechanic_matters": False,
         "confident": False}), H) == "advisory"
    # evidence bound to a DIFFERENT head is not evidence about this tree
    assert mechanic_gate_status(_FakeLedger(
        {"head": "other", "whitebox": "absent", "mechanic_matters": False,
         "confident": True}), H) == "advisory"
    # no evidence at all -> advisory (never invent a block)
    assert mechanic_gate_status(_FakeLedger({}), H) == "advisory"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && pytest tests/coding/test_spec40_white_box_oracle.py::test_gate_hierarchy_paths -v`
Expected: FAIL — `ImportError: cannot import name 'mechanic_gate_status'`.

- [ ] **Step 3: Implement `mechanic_gate_status`**

Follow `acceptance_gate_status`'s discipline exactly: READ-ONLY, and **fail-open** (any
read error, missing evidence, or head mismatch returns `"advisory"` — a `done`-block must
never be invented, per the module's existing note that a spurious block with no recovery is
the wedge SPEC-34's review forbids).

- [ ] **Step 4: Wire it into the done chokepoint**

In `runner._acceptance_gate_blocks_done`, after the existing `acceptance_gate_status`
handling, add the mechanic gate. It must return a **recoverable** refusal that routes
through the same F128 `completion_refused` ladder (so it is bounded and terminates in a
human-routed `completion_blocked`, never a silent wedge):

```python
    mech = mechanic_gate_status(store, head)
    if mech == "red":
        return ("the declared mechanic is not proven at master head "
                f"{head[:12]} — {mechanic_gate_reason(store)}. `done` is refused until "
                "it is proven: expose window.__probe.solution() and .won() (the probe "
                "fires your solution with the mechanic on — must win — and the same "
                "shot with it off — must NOT win), or fix the mechanic. The probe "
                "re-runs on the next merge and lifts this automatically (SPEC-40).")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd python && pytest tests/coding/ -v -k "spec40 or spec35 or completion"`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add python/errorta_council/coding/completion.py python/errorta_council/coding/runner.py python/tests/coding/test_spec40_white_box_oracle.py
git commit -m "feat(coding): SPEC-40 (E) — four-path mechanic gate hierarchy"
```

---

### Task 7: Regression-lock validation + full suite

**Files:**
- Test: `python/tests/coding/test_spec40_white_box_oracle.py`

- [ ] **Step 1: Add the explicit lock tests**

One test per regression lock in the spec, each named for its lock so a future reader can
map them: lock 1 (golf-2 blocks via path 3), lock 2 (golf-4 unblocks), lock 3 (golf-3 /
`spec38/grabaim` stays green), lock 4 (no marginal verdict drives a hard stop — assert
`_verdict_to_result(...).passed is True` for an uncertain differential AND that
`mechanic_gate_status` returns `advisory`), lock 6 (a non-web project is unaffected —
`has_web_profile` False short-circuits), lock 7 (both arms stamp the same components).

- [ ] **Step 2: Run the full coding suite**

Run: `cd python && pytest tests/coding/ -q`
Expected: PASS with no regressions.

- [ ] **Step 3: Run the live suite**

Run: `cd python && pytest tests/coding/ -q -m live -k spec40`
Expected: PASS.

- [ ] **Step 4: Lint**

Run: `cd python && ruff check errorta_council/coding/`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add python/tests/coding/test_spec40_white_box_oracle.py
git commit -m "test(coding): SPEC-40 — regression locks 1-7"
```
