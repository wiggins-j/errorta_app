# F152 — Delivery gate catches a web app that doesn't serve (HTTP assertion in the launch probe)

## Problem

A coding-council run delivered a Next.js app, `errorta runtime run --go` launched it,
and the browser showed a **compile error** — yet the run had already declared
`project_done`. The delivery review (F146) was supposed to prevent exactly this.
It didn't, and the reason is specific and grounded:

The F146 **launch probe** (`runtime_process.RuntimeProcessManager.launch_probe`,
`runner._delivery_launch_evidence`) is the piece that verifies a runnable head
"launches without crashing." Today it:

1. Spawns the `start` command headless + sandboxed.
2. Watches the **process** for a bounded window (`_LAUNCH_PROBE_SECONDS = 12s`).
3. Classifies **clean** if the process survived the window and the last 40 log
   lines do **not** contain the literal string `"Traceback (most recent call last)"`.

It makes **zero HTTP requests** (verified — `launch_probe` never calls
`probe_http`/`_probe`). For a web server this is blind:

- `npm run dev` binds its port and stays alive for the whole window → **survived**.
- A Next.js/TS **compile error is a Python-free failure** — the log tail never
  contains `"Traceback (most recent call last)"`, so `has_traceback` is `False`.
- Next.js dev **compiles lazily per route** — with nothing requesting the page,
  the compile error is never even triggered, let alone observed.

Result: `status="clean"` → `launched_clean=True` → delivery review passes → `done`.
The 500 only appears when a human finally opens the page.

Two independent gaps, one root cause (**the probe never talks HTTP to the app**):

- **G1 — no request is made**, so lazy-compiled frameworks never surface the error.
- **G2 — the only failure signal is a Python-specific traceback string**, so a
  non-Python stack (JS/TS/webpack "Failed to compile") is invisible even if printed.

Separately, the merge gate treats a **test-less** project as vacuously passing
(`runner.py:2318` `tests_ok = tests_passed OR not get_test_commands()`), so no
`next build`/`tsc` ever runs either. That's a real second hole but it's the user's
to close per-project (`errorta test-commands set`); this spec fixes the **default**
behavior so a non-serving web app is caught with **zero configuration**.

## Goal

For a runnable **HTTP-serving** profile, the delivery launch probe must **make a
real HTTP request to the app and require a healthy (2xx/3xx) response**. A server
that binds its port but only ever answers 5xx (the compile-error case) is a
**crash**, blocks `done`, and files a dev finding carrying the HTTP status + the
now-populated log tail (which, once a request has triggered compilation, contains
the framework's actual "Failed to compile …" message).

This is deliberately small: it reuses the existing `probe_http` (already
2xx/3xx-only), rides the existing observe loop, and flows into the existing
`crashed → file finding → re-open run` path. No new gate, no new command.

## Non-goals

- **Not** a full-route crawler. The probe hits the profile's declared health/demo
  URL(s) (`/` in practice) — it catches "the app doesn't load / errors on open",
  which is the reported failure. A compile error isolated to a deep route that is
  never requested is only fully caught by a build gate (see Follow-ups).
- **Not** auto-registering `next build`/`tsc` as a default test command. That is
  the strictly stronger, all-routes fix and is scoped as a follow-up (F153) because
  it needs per-ecosystem build detection; this spec ships the minimal HTTP
  assertion that closes the reported case now.
- No change to non-HTTP profiles (CLI/desktop): their `survived && !traceback`
  classification is unchanged.

## Design

All changes are in `python/errorta_council/coding/runtime_process.py`
(`launch_probe`), plus a small marker-scan helper. `_delivery_launch_evidence`
and `delivery_review` are unchanged — they already turn a `"crashed"` verdict
into a filed finding that blocks `done`.

### 1. Probe HTTP health during the window (G1)

Today the observe loop just sleeps until the deadline. Change it so that, **when
the profile has an HTTP health check** (`profile.health.type == "http"`), each
poll tick also issues `probe_http(health_url)` against the live port:

- The **first** poll that returns ok (2xx/3xx) sets `served_ok = True` and the
  probe may **exit early as clean** (the app is up and its primary route
  compiles + serves — a strictly stronger clean signal than "process alive").
- A poll that connects but returns **non-2xx/3xx** records `last_http = (status, detail)`
  but does **not** immediately fail — a cold server can 5xx briefly while warming.
  Keep polling.
- A connection-refused/timeout means "not up yet" — keep polling (unchanged wait).

`probe_http` already returns `(200 <= status < 400, str(status))`, so a 500
compile-error page yields `(False, "500")`. The request itself is what triggers
Next.js to compile the route, so it also **populates the log tail** with the
compile error for the finding detail.

Widen the window for HTTP-serving profiles: a JS dev server's first compile is
routinely 10–30s, so 12s is too short to fairly demand a served response. Use a
longer bound (proposed `_LAUNCH_HTTP_PROBE_SECONDS = 45.0`, still clamped ≤120)
**only** when `health.type == "http"`; non-HTTP profiles keep the 12s window.

### 2. Classification (uses the HTTP outcome)

At window end (or early exit), for an HTTP-serving profile:

| Observed within window | Verdict | Rationale |
|---|---|---|
| A 2xx/3xx at any point (`served_ok`) | **clean** | app is up and serves its route |
| Responses seen, **none ever 2xx/3xx** (`last_http` is 4xx/5xx) | **crashed** | binds a port but errors on load — the compile-error case |
| **No HTTP response at all** (only refused), process survived | **clean** (survival, as today) | avoid penalizing a genuinely slow/edge build — the 5xx path is what catches the real failure |
| Process exited non-zero / signal / Python traceback | **crashed** (as today) | unchanged |

The "responses seen but never healthy through a 45s window" branch is the new
catch. A transient warmup 500 resolves to a later 2xx → `served_ok` → clean, so
this does **not** flag a healthy-but-slow app. A real compile error returns 500
for the **whole** window → `crashed`.

The "never responded at all" branch is intentionally left as clean-by-survival:
making it a failure would risk false blocks on slow/odd builds, and it is not the
reported failure mode (a compile error **binds and 500s**, it does not refuse the
connection).

### 3. Enrich the finding via the shared crash-marker scan (defined in F153)

Once the HTTP request in step 1 triggers compilation, the framework prints its
actual error (`Failed to compile …`) to the log. When classifying a `crashed`
verdict from the HTTP path, include the matched line from the shared
`_has_crash_signature(tail)` helper introduced in **F153** (a small,
high-precision, framework-agnostic marker set that supersedes the Python-only
`"Traceback (most recent call last)"` check). F152 does not define its own marker
list — it consumes F153's — so the two specs share one crash-signature primitive.

### Detail / finding content

On a `crashed` verdict from the HTTP path, `detail` is:

```
<profile_id>: served HTTP 500 at http://127.0.0.1:<port>/ — not a healthy response
<the matched compile-error line, if any>
<log tail>
```

`delivery_review` files this under the existing `"fix delivery review"` /
`_delivery_launch_evidence` → crash → `store.add_task(... role=DEV ...)` path, so
the run re-opens (`_has_open_work`) and the team fixes the compile error. No new
wiring.

## Edge cases

- **App with no `/` route (pure API returning 404 on `/`)**: 404 is *not* 2xx/3xx,
  so a naive rule would false-fail. Mitigation: probe the profile's `demo.url`
  when present (the resolver sets a demo URL for the intended entrypoint); and
  treat **4xx as `served_ok`** for the "is it alive" purpose — a 4xx proves the
  server compiled and is routing (it answered), which is enough to clear the
  "doesn't serve" bar. Only **5xx** (and connection failure that never resolves to
  any response) is the compile/crash signal. Revised rule: `served_ok` = any
  response with `status < 500`; `crashed` = only-ever-≥500 through the window.
  (`probe_http` returns the numeric status string, so the classifier can threshold
  on 500 directly rather than reuse `probe_http`'s 2xx/3xx boolean.)
- **Health URL uses `{port}` placeholder**: substitute the live allocated port
  (`_sub_port(health.url, live.port)`), same as `_monitor`/`health_check` already do.
- **Non-HTTP health (`health.type != "http"`)**: no HTTP probe; classification is
  exactly today's `survived && !markers`. CLI/desktop unaffected.
- **Probe raises / httpx unavailable**: the HTTP call is best-effort and wrapped —
  a failure to *probe* must not fabricate a crash. If we can never get any HTTP
  response due to a probe-side error (not an app 5xx), fall back to survival
  classification (do not block on our own inability). Only an actual observed ≥500
  from the app blocks.
- **Sandbox networking**: the launch probe runs under the F039 sandbox with
  loopback networking allowed (dev servers must bind); the probe's `httpx.get`
  targets `127.0.0.1:<port>` on the host, which reaches the sandboxed child's bound
  port (same as the live `_monitor` health check already does). Confirm during
  implementation that the probe runs host-side, not inside the sandbox.
- **Window cost**: the 45s HTTP window is an upper bound with **early exit** on the
  first healthy response — a fast app clears in ~1 tick. Only a broken/slow app
  pays the full window, and only once per unchanged head (delivery review is
  cached by head).

## Testing

New cases in `python/tests/coding/test_f146_slice_c_launch.py` (a fake
HTTP-serving start command whose port behavior is scriptable, mirroring the
existing `_CLEAN_EXIT`/crash fixtures):

- **`test_http_500_through_window_is_crash`** — a server that binds and returns
  500 for the whole window → `status="crashed"`, detail mentions HTTP 500;
  `_delivery_launch_evidence` returns not-clean; `delivery_review` files a dev task
  and `done` is blocked.
- **`test_http_200_is_clean_and_early_exits`** — a server that serves 200 →
  `status="clean"`; verify it did not wait the full window (early exit).
- **`test_http_warmup_500_then_200_is_clean`** — 500 on the first poll(s), 200
  later → `clean` (no false positive on a slow warmup).
- **`test_http_4xx_is_clean`** — a server that only ever 404s (API with no `/`) is
  `clean` (it answered; it compiled and routes) — guards against false-failing APIs.
- **`test_http_never_responds_survives_is_clean`** — binds nothing/refuses the
  whole window but process survives → `clean` (unchanged survival behavior; no new
  false block).
- **`test_compile_marker_in_log_is_crash`** — a process that prints
  `Failed to compile` and stays up → `crashed` (G2 marker scan), independent of the
  Python-traceback path.
- **`test_non_http_profile_unchanged`** — a CLI/desktop profile classifies exactly
  as today (survival + Python-traceback only; no HTTP probe attempted).
- **Regression**: the existing Slice-C tests (`test_launch_clean_allows_done`,
  `test_launch_survives_startup_window_is_clean`, `test_launch_probe_crash`,
  `test_launch_probe_cli_nonzero_no_traceback_is_clean`, etc.) stay green — none
  use an HTTP health profile, so the new branch is inert for them.

## Documentation

- `docs/CLI.md` (delivery-review / runtime section): note that for a web/API
  project the delivery gate now **requests the app and requires it to serve** (a
  server that only errors on load blocks "done").
- A one-line pointer that a **full-compile** guarantee (all routes) comes from
  registering a build command: `errorta test-commands set --commands '["npm run build"]'`.

## Out of scope / follow-ups

- **F153 (stronger, all-routes):** auto-derive a build/typecheck command from the
  detected runtime (Node `build` script → `npm run build`; TS → `tsc --noEmit`;
  Python → import/compile smoke) and run it in the delivery gate when no test
  commands are registered — a production build fails on **any** route's compile
  error, not just the probed one. Bigger because it needs per-ecosystem detection.
- The autonomous "fix delivery review findings" **livelock cap**
  (`delivery_review_round_limit`) is a separate, already-identified follow-up — with
  this spec a real compile error will (correctly) re-open the run, which makes the
  round cap more relevant, not less.
```
