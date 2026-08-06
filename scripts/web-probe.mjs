// GL01 (Item 1) — the black-canvas liveness oracle.
//
// A standalone Node/Playwright probe the errorta engine (coding/web_probe.py)
// shells out to. Given a served URL + a rendered-frame count, it: opens the URL
// headless in Chromium, collects console.error + pageerror events, waits N
// rendered frames (requestAnimationFrame), screenshots the first <canvas> (or the
// viewport if none), and computes a NON-BLACK verdict (a uniform near-zero frame
// FAILS; a dark-but-varied frame PASSES). It prints exactly one JSON line to
// stdout: {"ok":bool,"console_errors":[...],"non_black":bool,"reason":"...",
// "screenshot":"<path|>"}.
//
// This script is the errorta engine's OWN trusted tool (NOT generated code — the
// generated app is the sandboxed server it points at). It resolves Playwright
// from errorta's node_modules; when Playwright/Chromium are unavailable it exits
// non-zero and the engine degrades to NO probe evidence (fail-open).
//
// Usage: node web-probe.mjs <url> [frames] [--screenshot <path>] [--timeout-ms N]

function parseArgs(argv) {
  const out = { url: "", frames: 30, screenshot: "", timeoutMs: 15000,
                legacySweep: false, whitebox: true };
  const rest = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--screenshot") out.screenshot = argv[++i] || "";
    else if (a === "--timeout-ms") out.timeoutMs = parseInt(argv[++i] || "15000", 10);
    // SPEC-40 escape hatches, passed through by web_probe.py from the policy knobs.
    // `--legacy-sweep` restores the geometry-anchored [0.8,1.3,2.0]xD powers exactly;
    // `--no-whitebox` skips the white-box phase entirely (today's emitted JSON).
    else if (a === "--legacy-sweep") out.legacySweep = true;
    else if (a === "--no-whitebox") out.whitebox = false;
    else rest.push(a);
  }
  if (rest[0]) out.url = rest[0];
  if (rest[1]) { const n = parseInt(rest[1], 10); out.frames = Number.isNaN(n) ? 30 : Math.max(0, n); }  // honor 0 (assert on first paint)
  return out;
}

async function importChromium() {
  // Prefer the repo's @playwright/test (its dev dep); fall back to the standalone
  // packages. Each re-exports the same `chromium` browser type.
  for (const mod of ["@playwright/test", "playwright", "playwright-core"]) {
    try {
      const m = await import(mod);
      if (m && m.chromium) return m.chromium;
    } catch { /* try the next */ }
  }
  return null;
}

// Mean luminance + variance over an RGBA byte buffer. A uniform near-zero frame
// has ~0 mean and ~0 variance (BLACK); a dark-but-varied frame has non-trivial
// variance (LIVE). Alpha is ignored (a transparent canvas over black is still
// "rendered nothing").
function stats(rgba) {
  let n = 0, sum = 0, sumSq = 0;
  for (let i = 0; i + 3 < rgba.length; i += 4) {
    const lum = 0.299 * rgba[i] + 0.587 * rgba[i + 1] + 0.114 * rgba[i + 2];
    sum += lum; sumSq += lum * lum; n++;
  }
  if (n === 0) return { mean: 0, variance: 0, samples: 0 };
  const mean = sum / n;
  return { mean, variance: sumSq / n - mean * mean, samples: n };
}

// A dark scene is legitimate; a UNIFORM near-zero one is the black-canvas bug.
const LUM_THRESHOLD = 8.0;   // mean luminance (0..255) that alone clears the bar
const VAR_THRESHOLD = 4.0;   // pixel variance that clears the bar even when dark

function emit(obj) { process.stdout.write(JSON.stringify(obj) + "\n"); }

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.url) { emit({ ok: false, non_black: false, console_errors: [], reason: "no url", screenshot: "" }); return; }
  const chromium = await importChromium();
  if (!chromium) { emit({ ok: false, non_black: false, console_errors: [], reason: "playwright unavailable", screenshot: "" }); process.exitCode = 3; return; }

  let browser = null;
  const consoleErrors = [];
  try {
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(String(m.text()).slice(0, 500)); });
    page.on("pageerror", (e) => consoleErrors.push(String(e && e.message || e).slice(0, 500)));
    await page.goto(args.url, { waitUntil: "load", timeout: args.timeoutMs });

    // Wait N ACTUALLY-RENDERED frames (not wall-clock), bounded by the timeout.
    await page.evaluate(
      (n) => new Promise((resolve) => {
        let left = n;
        const tick = () => (--left <= 0 ? resolve() : requestAnimationFrame(tick));
        requestAnimationFrame(tick);
      }),
      args.frames
    ).catch(() => {});

    // SPEC-30 (S1) — the INTERACTION phase. Passive load catches a black canvas
    // and a crash-on-start, but the gravity-golf failures (a module whose method
    // name the integration got wrong; an input handler wired to the wrong
    // contract) only fault when the artifact is DRIVEN. So: hash the canvas, drive
    // a realistic pointer gesture across it (a press-drag-release — a "shot" for a
    // mouse game, a generic poke otherwise), let it settle, and re-hash. A new
    // pageerror during this window (the `applyGravity is not a function` crash) is
    // captured by the existing pageerror listener and fails `ok`; an UNCHANGED
    // canvas (the empty gradient that ignores input) sets interaction_changed=false
    // for the gate to reject. Fully guarded: if we cannot interact, the fields go
    // null and the passive verdict stands (fail-open, never a false red).
    let interactionChanged = null;
    let hookPresent = false;   // SPEC-38: a probe hook exposed the ball for targeting
    const errsBeforeInteract = consoleErrors.length;
    try {
      const hashCanvas = () => page.evaluate(() => {
        const c = document.querySelector("canvas");
        if (!c || !c.width || !c.height) return null;
        try {
          const g = c.getContext("2d") || c.getContext("webgl") || c.getContext("webgl2");
          if (!g || !g.getImageData) return null;
          const d = g.getImageData(0, 0, c.width, c.height).data;
          // Cheap, stable digest: strided sum so a moving sprite shifts it.
          let h = 0;
          for (let i = 0; i < d.length; i += 40) h = (h + d[i] * (i + 1)) % 2147483647;
          return h;
        } catch { return null; }
      }).catch(() => null);

      // SPEC-38: fetch the canvas geometry AND (if the game exposes a probe hook)
      // the ball's position, so the gesture can be TARGETED at the ball. A
      // fixed-location gesture misses the ball in a grab-to-aim ("positional")
      // control and false-reds a playable game (run-3). cw/ch are the canvas
      // intrinsic pixels; ball is in that same space and must be mapped to viewport
      // CSS px via the rect ratio (a CSS-scaled / hi-dpi canvas would otherwise be
      // missed).
      const geo = await page.evaluate(() => {
        const c = document.querySelector("canvas");
        if (!c) return null;
        const r = c.getBoundingClientRect();
        const g = { x: r.left, y: r.top, w: r.width, h: r.height,
                    cw: c.width, ch: c.height };
        const P = window.__probe;
        if (P && typeof P.state === "function") {
          try {
            const s = P.state();
            if (s && s.ball && typeof s.ball.x === "number") {
              g.ballX = s.ball.x; g.ballY = s.ball.y;
            }
          } catch { /* no ball -> blind gesture */ }
        }
        return g;
      }).catch(() => null);

      const waitFrames = (n) => page.evaluate(
        (k) => new Promise((resolve) => {
          let left = k;
          const tick = () => (--left <= 0 ? resolve() : requestAnimationFrame(tick));
          requestAnimationFrame(tick);
        }), n).catch(() => {});

      // SPEC-38: a probe hook is present if state() is callable — independent of ball
      // readability, so a hook-present-but-ball-unreadable artifact never gets the
      // no-hook "inert" reason (review lock #5).
      hookPresent = await page.evaluate(
        () => !!(window.__probe && typeof window.__probe.state === "function")
      ).catch(() => false);

      if (geo && geo.w > 0 && geo.h > 0) {
        const before = await hashCanvas();
        const clampX = (v) => Math.min(Math.max(v, geo.x + 4), geo.x + geo.w - 4);
        const clampY = (v) => Math.min(Math.max(v, geo.y + 4), geo.y + geo.h - 4);
        const hasBall = typeof geo.ballX === "number" && geo.cw > 0 && geo.ch > 0;
        let moved = null;

        if (hasBall) {
          // SPEC-38 (drift-subtracted, review-hardened): the human pointer path is
          // judged by whether a ball-TARGETED trusted gesture moves the ball
          // MATERIALLY MORE than it drifts on its own — with the mechanic left in its
          // NORMAL (on) state. This depends on NOTHING about setMechanic (a game may
          // couple all integration to that flag), so it neither false-fails such a
          // game nor false-passes a dead mouse whose gravity ignores the flag.
          const readBall = () => page.evaluate(() => {
            try { const s = window.__probe.state(); return { x: s.ball.x, y: s.ball.y }; }
            catch { return null; }
          }).catch(() => null);
          const doGesture = async (start) => {
            const px = clampX(geo.x + start.x * (geo.w / geo.cw));
            const py = clampY(geo.y + start.y * (geo.h / geo.ch));
            const x1 = clampX(px + geo.w * 0.25), y1 = clampY(py - geo.h * 0.15);
            await page.mouse.move(px, py);
            await page.mouse.down();
            await page.mouse.move((px + x1) / 2, (py + y1) / 2, { steps: 8 });
            await page.mouse.move(x1, y1, { steps: 8 });
            await page.mouse.up();
          };
          const W = Math.max(20, args.frames);
          // control run: reset, NO gesture, measure baseline drift over W frames.
          let drift = null;
          await page.evaluate(() => { try { window.__probe.reset(); } catch { /* */ } });
          let b0 = await readBall();
          if (b0) { await waitFrames(W); const b1 = await readBall();
            if (b1) drift = Math.hypot(b1.x - b0.x, b1.y - b0.y); }
          // gesture run: reset, TARGETED gesture at the ball, measure over W frames.
          let withG = null;
          await page.evaluate(() => { try { window.__probe.reset(); } catch { /* */ } });
          const g0 = await readBall();
          if (g0) { await doGesture(g0); await waitFrames(W); const g1 = await readBall();
            if (g1) withG = Math.hypot(g1.x - g0.x, g1.y - g0.y); }
          if (drift !== null && withG !== null) {
            // the gesture must add substantial motion beyond drift (launch >> drift).
            moved = withG > drift + 20 && withG > drift * 2;
          }
        }

        if (moved === null) {
          // No usable hook/ball: the SPEC-30 blind gesture + canvas-hash verdict.
          const px = geo.x + geo.w * 0.35, py = geo.y + geo.h * 0.55;
          const x1 = geo.x + geo.w * 0.6, y1 = geo.y + geo.h * 0.4;
          await page.mouse.move(px, py);
          await page.mouse.down();
          await page.mouse.move((px + x1) / 2, (py + y1) / 2, { steps: 8 });
          await page.mouse.move(x1, y1, { steps: 8 });
          await page.mouse.up();
          await page.mouse.click(geo.x + geo.w * 0.5, geo.y + geo.h * 0.5);
          await waitFrames(Math.max(30, args.frames));
          const after = await hashCanvas();
          interactionChanged = (before !== null && after !== null) ? before !== after : null;
        } else {
          interactionChanged = moved;
        }
      }
    } catch { /* interaction is best-effort; passive verdict stands */ }
    const interactionError = consoleErrors.length > errsBeforeInteract;

    // SPEC-37: behavioral mechanic oracle (DIFFERENTIAL). The interaction phase
    // above only proves the canvas RESPONDS to input — an inert-gravity ball still
    // moves, so a regular-golf game passes it. To prove a declared mechanic has
    // EFFECT, a game whose DoD forbids straight-line solutions must expose a hook:
    //   window.__probe = {
    //     state:       () => ({ball:{x,y}, hole:{x,y,r}, wells:[...], moving:bool}),
    //     shoot:       (dx,dy,power) => {},  // launch the ball in a direction/power
    //     tick:        (n) => {},            // advance n FIXED steps (deterministic)
    //     reset:       () => {},             // return the ball to the tee (REQUIRED)
    //     setMechanic: (on) => {},           // enable/disable the mechanic (REQUIRED)
    //   }
    // Why DIFFERENTIAL and not "a straight shot must miss": an absolute win-condition
    // depends on the game's (unknown) power cap and hole geometry — a super-max
    // straight shot reaches any straight-clear hole regardless of gravity, and an
    // on-axis well never deflects by symmetry (both would false-fail a live game).
    // Instead: fire the SAME straight shot at the hole with the mechanic ON vs OFF;
    // if the outcome is materially DIFFERENT (different sink result, or endpoints
    // apart by > hole.r) at ANY swept power, the mechanic MATTERS. If ON and OFF are
    // identical at every power, it is inert. This is power-cap- and geometry-
    // independent. Hardened against a gamed hook: sinking + movement are computed
    // HERE from ball/hole coordinates (never a game flag), a no-op shoot / missing
    // state / non-resetting reset / no-op setMechanic is ran=false (web_probe.py
    // fails it as UNUSABLE, not a free pass), and the phase is time-boxed. Folded
    // only for the MASTER arm of a project that DECLARES straight-shots-must-fail.
    let mechanicProbe = {
      has_hook: false, ran: false, wells: 0, mechanic_matters: null,
      powers: [], reason: "",
      // SPEC-40 (item A/B): the calibration + confidence metadata. `p_sink` is the
      // bisected minimum power at which a mechanic-OFF straight shot reaches the hole
      // (null when the bracket never converged); `confident` is the four-part rule
      // that decides whether an INERT verdict may hard-block (item E path 3) or is
      // advisory only (path 4); `straight_sank` is the SEPARATE straight-line claim.
      p_sink: null, confident: false, max_gap: 0, straight_sank: null };
    try {
      const hasHook = await page.evaluate(() => {
        const p = window.__probe;
        return !!(p && typeof p.state === "function" && typeof p.shoot === "function"
                  && typeof p.tick === "function" && typeof p.reset === "function"
                  && typeof p.setMechanic === "function");
      }).catch(() => false);
      mechanicProbe.has_hook = hasHook;
      if (hasHook) {
        const withTimeout = (pr, ms) => Promise.race([
          pr, new Promise((res) => setTimeout(() => res({ ran: false,
            reason: "mechanic phase timed out" }), ms))]);
        // SPEC-39 INVARIANT (do not break): this differential MUST stay within ONE
        // synchronous page.evaluate — a sync evaluate blocks the page's rAF /
        // setTimeout / setInterval callbacks, so the game's own loop is frozen for
        // the whole on/off/off2 sweep and tick() is the sole driver. If this is ever
        // split or made to `await` mid-sweep, a per-shot pause/determinism guarantee
        // must be restored (the pause clause SPEC-39 removed relied on this).
        const r = await withTimeout(page.evaluate((legacySweep) => {
          const P = window.__probe;
          const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
          // Take control + go to a canonical start; the SPEC-30 interaction phase
          // ran first, so read the tee AFTER a reset, not from the drifted state.
          P.reset();
          const s0 = P.state();
          const wells = (s0 && Array.isArray(s0.wells)) ? s0.wells.length : 0;
          if (!s0 || !s0.ball || !s0.hole || typeof s0.hole.r !== "number") {
            return { wells, ran: false, reason: "state() lacks ball/hole/radius" };
          }
          const tee = { x: s0.ball.x, y: s0.ball.y };   // primitive snapshot (no aliasing)
          const D = dist(tee, s0.hole);
          if (!(D > 0)) return { wells, ran: false, reason: "ball already at hole" };
          // Pass a NORMALIZED direction + a separate power (speed), so a game that
          // does vx = dx*power gets a sane launch speed regardless of |tee->hole|.
          const dir = { x: (s0.hole.x - tee.x) / D, y: (s0.hole.y - tee.y) / D };
          const holeR = s0.hole.r;
          // Fire the SAME straight shot at a given power with the mechanic on/off and
          // simulate to rest; returns {sank, end:{x,y}, maxMove}. sink + movement +
          // rest are all GEOMETRIC (never a game flag): the loop exits when the ball
          // stops moving over a window of ticks, so a wrong state().moving cannot cut
          // the observation short. Coordinates are snapshotted as primitives so a
          // state() that returns a live ball reference cannot zero the movement metric.
          const runShot = (power, on) => {
            P.reset();
            const st = P.state().ball;
            const start = { x: st.x, y: st.y };
            if (dist(start, tee) > 2) return null;   // reset() failed / no-op
            P.setMechanic(on);
            P.shoot(dir.x, dir.y, power);
            let sank = false, minD = D, maxMove = 0;
            let prev = { x: start.x, y: start.y }, still = 0;
            let end = { x: start.x, y: start.y };
            let i = 0;
            for (; i < 6000; i++) {
              P.tick(1);
              const b = P.state().ball;
              const cur = { x: b.x, y: b.y };
              end = cur;
              maxMove = Math.max(maxMove, dist(cur, start));
              minD = Math.min(minD, dist(cur, s0.hole));
              if (minD <= holeR) { sank = true; break; }
              // geometric rest: still for several consecutive ticks
              still = (dist(cur, prev) < 0.05) ? still + 1 : 0;
              prev = cur;
              if (still >= 20) break;
            }
            return { sank, end, maxMove, ticks: i };
          };

          // SPEC-40 (item A) — CALIBRATE the sweep to the GAME's usable power range.
          //
          // The old sweep anchored to hole GEOMETRY (`[0.8,1.3,2.0] x D`) and fed the
          // result to a `shoot()` that means a SPEED. On gravity-golf-4 that launched
          // the ball at 28,800 px/tick: it crossed the whole 600px course in ~one tick,
          // before gravity could integrate, so ON was identical to OFF and a live
          // mechanic was reported inert. The mechanic demonstrably mattered — but only
          // in a power band the sweep never sampled.
          //
          // So: BISECT for `P_sink`, the minimum power at which a mechanic-OFF straight
          // shot first reaches the hole. This is sound precisely because the OFF arm is
          // MONOTONIC in power — with the mechanic disabled the ball travels a straight
          // line at the hole and its reach grows with launch speed, so "does it sink"
          // flips exactly once. The ON arm is NOT monotonic (that is the mechanic's
          // whole point), which is why the bracket must be built on OFF.
          let ticks = 0;
          const TICK_BUDGET = 400000;
          const sinksOff = (p) => {
            const r = runShot(p, false);
            if (r === null) return null;
            ticks += r.ticks;
            return r.sank;
          };
          let lo = 0, hi = 0, bracketed = false, resetOk = true;
          if (!legacySweep) {
            for (let p = 1, i = 0; i < 14 && ticks < TICK_BUDGET; i++, p *= 2) {
              const s = sinksOff(p);
              if (s === null) { resetOk = false; break; }
              if (s) { hi = p; bracketed = true; break; }
              lo = p;
            }
          }
          let pSink = null;
          if (bracketed) {
            for (let i = 0; i < 12 && ticks < TICK_BUDGET; i++) {
              const mid = (lo + hi) / 2;
              const s = sinksOff(mid);
              if (s === null) { resetOk = false; break; }
              if (s) hi = mid; else lo = mid;
              if ((hi - lo) / hi < 0.01) break;
            }
            pSink = hi;
          }
          // Sweep the band AT and BELOW P_sink: that is where the ball is slow enough
          // for the mechanic to have integration time to bend it. Log-spaced over
          // [0.15, 1.5] x P_sink. With no bracket we fall back to the old geometry
          // sweep AND report `confident:false`, so the verdict can only be advisory.
          const powers = (pSink && !legacySweep)
            ? Array.from({ length: 7 },
                (_, i) => pSink * 0.15 * Math.pow(10, i / 6))
            : [0.8, 1.3, 2.0].map((k) => k * D);

          let matters = false, anyMoved = false, nondet = false;
          let completed = 0, maxGap = 0, greyBand = false, straightSank = false;
          if (resetOk) {
            for (const pow of powers) {
              if (ticks > TICK_BUDGET) break;
              const on = runShot(pow, true);
              const off = runShot(pow, false);
              const off2 = runShot(pow, false);   // determinism/causation guard
              if (on === null || off === null || off2 === null) { resetOk = false; break; }
              ticks += on.ticks + off.ticks + off2.ticks;
              if (on.maxMove > 2 || off.maxMove > 2) anyMoved = true;
              // If two IDENTICAL (off) shots diverge, the game is nondeterministic —
              // the on/off difference cannot be attributed to the mechanic. Bail.
              if (off.sank !== off2.sank || dist(off.end, off2.end) > holeR / 2) {
                nondet = true; break;
              }
              completed++;
              // A straight-at-hole shot that sinks WITH the mechanic on is a
              // straight-line solution — a DISTINCT claim from "the mechanic matters"
              // (both can be true at once), so it is tracked separately and never
              // derived from `matters`.
              if (on.sank) straightSank = true;
              const gap = dist(on.end, off.end);
              maxGap = Math.max(maxGap, gap);
              if (on.sank !== off.sank || gap > holeR) matters = true;
              else if (gap > holeR / 2) greyBand = true;  // near-threshold -> uncertain
            }
          }
          P.setMechanic(true);   // restore
          if (!resetOk) return { wells, ran: false, reason: "reset() did not return the ball" };
          if (nondet) return { wells, ran: false, reason: "non-deterministic (two identical shots diverged) — tick()/shoot() must be deterministic for headless verification" };
          if (!anyMoved) return { wells, ran: false, reason: "shoot() did not move the ball" };
          // SPEC-40 (item B) — the CONFIDENCE rule. An INERT verdict may hard-block
          // delivery (item E path 3) only when all four hold; anything else is
          // advisory (path 4). The failure mode is therefore a MISSED block, never a
          // false red — which is the property golf-4 needed and did not have.
          // In LEGACY mode the confidence concept does not apply — today's trace
          // hard-gates the differential unconditionally, and the 3-power sweep could
          // never satisfy `completed >= 5`. So legacy reports confident, preserving
          // the escape hatch's "reproduces today exactly" contract.
          const confident = legacySweep
            || (bracketed && completed >= 5 && !greyBand && !nondet
                && ticks <= TICK_BUDGET);
          return { wells, ran: true, mechanic_matters: matters,
                   powers: powers.map((p) => Math.round(p * 100) / 100),
                   p_sink: pSink === null ? null : Math.round(pSink * 100) / 100,
                   confident, max_gap: Math.round(maxGap),
                   straight_sank: straightSank };
        }, args.legacySweep).catch(() => ({ ran: false, reason: "mechanic phase threw" })), 20000);
        Object.assign(mechanicProbe, r);
      }
    } catch { /* best-effort; web_probe.py treats an absent hook as no_hook */ }

    // Screenshot for the record (best-effort) + compute the verdict.
    let st = { mean: 0, variance: 0, samples: 0 };
    const hasCanvas = await page.evaluate(() => !!document.querySelector("canvas")).catch(() => false);
    if (hasCanvas) {
      // Read the first canvas's pixels in-page (the true black-canvas oracle).
      const px = await page.evaluate(() => {
        const c = document.querySelector("canvas");
        if (!c || !c.width || !c.height) return { w: c ? c.width : 0, h: c ? c.height : 0, data: null };
        try {
          const g = c.getContext("2d") || c.getContext("webgl") || c.getContext("webgl2");
          if (g && g.getImageData) {
            const d = g.getImageData(0, 0, c.width, c.height).data;
            return { w: c.width, h: c.height, data: Array.from(d) };
          }
        } catch { /* tainted / webgl — fall through to a screenshot analysis */ }
        return { w: c.width, h: c.height, data: null };
      }).catch(() => ({ w: 0, h: 0, data: null }));
      if (px.w === 0 || px.h === 0) {
        // A zero-size canvas is the gravity-golf defect itself.
        st = { mean: 0, variance: 0, samples: 0 };
      } else if (px.data) {
        st = stats(px.data);
      } else {
        st = await analyzeScreenshot(page, "canvas");
      }
    } else {
      st = await analyzeScreenshot(page, "viewport");
    }

    if (args.screenshot) {
      try { await page.screenshot({ path: args.screenshot }); } catch { /* best-effort */ }
    }

    const nonBlack = st.samples > 0 && (st.mean >= LUM_THRESHOLD || st.variance >= VAR_THRESHOLD);
    let reason;
    if (st.samples === 0) reason = hasCanvas ? "canvas has zero size (rendered nothing)" : "no renderable content";
    else if (nonBlack) reason = `rendered content (mean=${st.mean.toFixed(1)}, var=${st.variance.toFixed(1)})`;
    else reason = `frame is uniformly black (mean=${st.mean.toFixed(1)}, var=${st.variance.toFixed(1)})`;
    const ok = nonBlack && consoleErrors.length === 0;
    // SPEC-30: surface the interaction outcome. `interaction_changed` is
    // true/false when we drove a gesture, null when we could not. `reason` gains a
    // clause so `gate_state.latest_gate_text` shows the reviewer WHY it failed.
    let reason2 = reason;
    if (interactionError) reason2 += "; crashed on interaction (see console)";
    else if (interactionChanged === false) reason2 += hookPresent
      ? "; the ball-targeted pointer gesture moved nothing — the mouse control path (mousedown/aim/shoot) appears unwired; the probe hook works but a human cannot play with the mouse"
      : "; canvas did not respond to input (inert)";
    else if (interactionChanged === true) reason2 += "; responded to input";
    emit({
      ok, non_black: nonBlack, console_errors: consoleErrors, reason: reason2,
      screenshot: args.screenshot || "",
      interaction_changed: interactionChanged, interaction_error: interactionError,
      mechanic_probe: mechanicProbe,
    });
  } catch (e) {
    emit({ ok: false, non_black: false, console_errors: consoleErrors, reason: `probe error: ${String(e && e.message || e).slice(0, 300)}`, screenshot: args.screenshot || "" });
    process.exitCode = 4;
  } finally {
    if (browser) { try { await browser.close(); } catch { /* ignore */ } }
  }
}

// Analyze a Playwright screenshot by drawing it back into an offscreen canvas
// inside the page (no manual PNG decode) and reading its pixels.
async function analyzeScreenshot(page, target) {
  try {
    let buf;
    if (target === "canvas") {
      const el = await page.$("canvas");
      buf = el ? await el.screenshot() : await page.screenshot();
    } else {
      buf = await page.screenshot();
    }
    const b64 = buf.toString("base64");
    const px = await page.evaluate(async (dataUri) => {
      const img = new Image();
      await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = dataUri; });
      const cv = document.createElement("canvas");
      cv.width = img.naturalWidth || img.width; cv.height = img.naturalHeight || img.height;
      const g = cv.getContext("2d");
      g.drawImage(img, 0, 0);
      const d = g.getImageData(0, 0, cv.width, cv.height).data;
      return Array.from(d);
    }, `data:image/png;base64,${b64}`);
    return stats(px);
  } catch {
    return { mean: 0, variance: 0, samples: 0 };
  }
}

main();
