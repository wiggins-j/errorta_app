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
  const out = { url: "", frames: 30, screenshot: "", timeoutMs: 15000 };
  const rest = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--screenshot") out.screenshot = argv[++i] || "";
    else if (a === "--timeout-ms") out.timeoutMs = parseInt(argv[++i] || "15000", 10);
    else rest.push(a);
  }
  if (rest[0]) out.url = rest[0];
  if (rest[1]) out.frames = Math.max(1, parseInt(rest[1], 10) || 30);
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
    emit({ ok, non_black: nonBlack, console_errors: consoleErrors, reason, screenshot: args.screenshot || "" });
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
