# Slack Run Control — Design (Slice 2)

**Date:** 2026-08-17
**Status:** Design (autonomous continuation of the studio-manager slices). Not yet implemented.
**Depends on:** merged Slack PM bridge + studio manager (Slice 1).
**Module:** extends `python/errorta_slack/tools.py` (the per-project tool surface).

---

## 1. Problem

A project created from Slack (Slice 1) is **idle** — you can chat its PM but can't
make the team actually work. Slice 2 lets you, in the **project's channel**, tell the
PM to **start the run** ("go build it"), **stop** it, and see **run status**.

## 2. Grounded facts (from the run-control mechanics investigation)

- **Start:** `errorta_app.routes.coding._start_run(project_id, {}, continue_=True)` —
  module-level, not async, not origin-gated in the function (the gate is only in the
  HTTP wrapper). Non-blocking: spawns a **daemon thread** owned by the sidecar that
  keeps running after the Slack turn returns; returns `{"started": True}`. This is the
  identical call the app's own PM path makes (`routes/coding.py:1846`). Preconditions:
  a team present (Slice-1 sets it) — `continue_=True` recovers the saved team and
  bypasses the fresh-start `run_setup_confirmed` gate. A member-health preflight may
  409 if a model/CLI provider is logged out.
- **Start SPENDS real money** (the team makes model calls every turn). → `start_run`
  is **C-class** (verified-button confirm), like `spend_cloud`/`publish_pr`.
- **Caps** (surfaced in the confirmation, not a dollar limit — the engine has none):
  `max_iterations` (default 200, terminal backstop), `max_model_calls` (None=unlimited).
- **Stop:** `LedgerStore(project_id).set_run_state(cancel_requested=True)` — graceful
  (the worker checks it at turn boundaries), survives restart. **R-class** (safe).
- **Status:** `LedgerStore(project_id).get_run_state().get("status") or "idle"` →
  `idle | running | stopped | failed | interrupted`. Layer onto the existing
  `project_status` reads (`team_log` + `attention`). No model call.
- **Do NOT** use `RunControl`/`run_store` (council deliberation subsystem, unrelated).
  **Do NOT** host a run loop in the bridge or call `CodingRunner.run` directly (the
  route layer owns lifecycle; direct calls double-write run_state).

## 3. Design

Extend the **per-project** tool surface (`errorta_slack/tools.py`) — run control is
per-project, exercised in the project's channel by the existing per-project concierge.
No new module; reuse the concierge, the confirmation machinery, and `handle_interaction`.

**New/changed verbs:**

| Verb | Trust | Effect |
|------|:---:|--------|
| `start_run` | **C** | `deps.start_run_fn(project_id)` → starts the coding team (spends). Confirmation summary states it will run the team and spend model calls up to the `max_iterations` cap. |
| `stop_run` | R | `deps.ledger_factory(project_id).set_run_state(cancel_requested=True)` — graceful cancel at the next turn boundary. |
| `project_status` | R | **extended** to include the run lifecycle state (`get_run_state()["status"]`) alongside the existing task/blocker summary. |

**Naming:** distinct from the existing `launch_runtime`/`stop_runtime` verbs — those
start/stop the F101 **preview process** (a dev server), NOT the coding run. Verb
summaries make the distinction explicit ("start the coding team working" vs "launch a
preview of the built code").

**Injectable seams (for egress-free tests — no real run ever starts in tests):**
- `ToolDeps.start_run_fn` — default a thin lazy wrapper: `def _default_start_run(pid):
  from errorta_app.routes.coding import _start_run; return _start_run(pid, {}, continue_=True)`.
  Import is INSIDE the wrapper (keeps `tools.py` free of a top-level `errorta_app.routes`
  import + preserves optionality). Tests inject a recording fake.
- Stop + status use the already-present `deps.ledger_factory` (LedgerStore) —
  `set_run_state` / `get_run_state`.

**Trust / injection guard (non-negotiable):** `start_run` executes ONLY from a verified
Slack `block_actions` payload — never from concierge text. Untrusted Slack text can't
start a spending run. Same discipline + tests as the other C-class verbs; the existing
`handle_interaction` confirmation path already routes per-project C verbs.

## 4. Error handling

- **start_run 409 "already in progress":** surface a friendly "the team is already
  working on it" (swallow the benign 409, like the app's PM path does).
- **member-health preflight 409:** surface "can't start — a model/CLI provider looks
  logged out: `<reason>`; check it and try again", not a crash.
- **stop_run when idle:** setting `cancel_requested` on an idle project is harmless;
  reply "nothing running to stop."
- Any `start_run_fn` exception → clean `{"status":"error","detail":<redacted>}`, no
  uncaught raise into a live turn (mirror the studio create-error handling).

## 5. Testing (egress-free; inject `start_run_fn`, `ledger_factory`)

- **Injection:** `start_run` with `confirmed_via=None` → `needs_confirmation`,
  `start_run_fn` NOT called; with `"block_actions"` → `start_run_fn` IS called. The #1 test.
- `stop_run` → `ledger_factory(pid).set_run_state(cancel_requested=True)` called;
  returns stopped/acked. Idle → friendly no-op.
- `project_status` includes the run lifecycle status from a fake `get_run_state`.
- start_run_fn raising (409-shaped / member-health / arbitrary) → clean error, no crash.
- Anti-drift canary (existing) stays green with the new verbs; a test asserts
  `start_run` is trust "C" and `stop_run` is "R".
- Optionality: `tools.py` still imports no `errorta_app.routes` at module load (the
  `_start_run` import is inside the default wrapper).

## 6. Deferred (not Slice 2)

- Pause/resume (the coding-run engine has no pause — only cancel; not inventing one).
- USD spend caps (engine has none; we surface iteration/call caps only).
- Mid-run team reconfig, archive/spin-down (Slice 3).
- Auto-report of run completion into the channel (the outbound poller could later post
  "run finished" — a nice follow-up, not Slice 2).
