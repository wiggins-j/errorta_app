# Run Analysis — Gravity Golf (2026-07-31), runs 5–9

The first genuine end-to-end exercise of the SPEC-29/30 execution loop outside a
hermetic fixture. Five real autonomous runs on a dev-venv sidecar (the only build
where GL01's `web:probe` can fire — a packaged binary has no repo tree). Each run
peeled back one layer; each layer got a fix.

## Headline

- **Four consecutive runs (6, 7, 8, 9) produced a working, playable gravity-golf
  game** — renders course/ball/hole, zero console errors, responds to mouse input
  — every one verified out-of-band by an independent headless probe (strokes
  incremented on a real drag), not just by the run's own signals. Runs 4 and
  earlier had never produced anything but a black screen or an empty "big square".
- **GL01's `web:probe` fired in a real run for the first time in the product's
  history** — per-PR, in-loop, and at delivery — and discriminated working-vs-inert
  both directions live.
- **No run reached formal `definition_of_done`.** Each stopped short on a *different*
  completion-gate defect; every one is now fixed except the last, which is a deeper
  PM-convergence problem, not a gate wedge.

## The layer-by-layer arc

| Run | Outcome | Root cause | Fix (commit) |
|---|---|---|---|
| 5 | stopped `gate_not_improving` | per-PR probe recorded a red test-run at each branch head → became the "latest gate run" (score 0) → tripped the acceptance-churn detector on branch-scoped evidence | exclude per-PR probes from gate scoring (`3ac1670`) |
| 6 | working game; stopped `planning_churn` | delivery review rejected the whole finished project as "diff truncated → split the delivered change" — unsatisfiable for a complete deliverable | truncation is a note, not an auto-reject; defer whole-artifact correctness to the execution gate (`23e8f9f`) |
| 6 | (same) | web:probe (a 0/1 liveness signal) scored on the acceptance-command axis → mid-build inert integration tripped `gate_not_improving` | exclude the master/delivery probe too — it has its own S2/per-PR enforcement (`23e8f9f`) |
| 7 | working game; stopped `planning_churn` | delivery reviewer saw only the diff → invented a filename ("Missing acceptance test file test/test.js") → DEV correctly refused to fabricate (SPEC-25 block) → human-required wedge | ground the delivery reviewer in the delivered tree; never invent paths (`47dac0e`) |
| 8 | working game; stopped `planning_churn` | a "fix the acceptance gate failures" task filed when the gate was red went moot (gate green before a DEV worked it); a blocked/human-required task cannot be auto-closed → permanent wedge | drop a moot engine-filed fix task when no failing evidence remains at the delivered head (`e4d974c`) |
| 9 | working game; **cancelled** iter 76 | the PM **over-plans**: backlog grew 57 → 84 → 96 → 114 → 144 while ~15 PRs merged; it never reaches a completion claim. An operator `interject` to "stop expanding scope and claim done" did not halt it. | **open — the next thing to fix** |

Under SPEC-30 the execution gate did its job throughout: it blocked broken
artifacts, grounded the reviewer with per-PR runtime evidence, and caught a branch
that regressed to a black canvas (mean=0.0) while master stayed green. The
completion-gate wedges (runs 5–8) were all unsatisfiable-constraint bugs of the
exact class the autonomy batch exists to kill (G1) — each fixed, each with a
regression test, full coding suite green at **2475**.

## The open finding (run 9): the PM does not converge

With every gate wedge removed, the run *still* did not finish — for a reason
upstream of the gate. The PM keeps planning polish, extra levels, and features
faster than they land, so the backlog never drains to the end-game that would
trigger a completion claim. An authoritative `interject` ("the DoD is met — stop
expanding scope, audit the checklist, claim done") was delivered and "applied" but
the backlog kept growing (114 → 144) with no completion attempt.

This is a distinct problem from the completion gate: the gate is now ready to
judge a `done` claim fairly, but the PM never makes one. It is a convergence /
plan-budget gap — the PM lacks a mechanism (or the discipline) to decide "the
checkable DoD criteria are satisfied; stop adding scope and deliver." Candidate
fixes, not yet built:

- A **plan budget / scope freeze**: once the DoD's mechanically-checkable criteria
  are green (renders, responds, N levels present, acceptance test present), clamp
  planning to integration/verification only and route to a completion claim —
  generalising SPEC-27's `clamp_planning` from a churn remedy to a convergence
  driver.
- A **DoD-checklist gate in the PM prompt**: surface which DoD items are already
  satisfied on master, so "more could be added" stops reading as "not done yet".
- Treat a still-growing backlog late in a run as a convergence alarm, not health.

## What this batch does NOT change

- **Known-open #1** (no engine path registers a UNIT-scoped test command → the
  TESTER seat is structurally empty). SPEC-30 closes the *practical* intent —
  execution grounding is now in the loop via the web:probe (per-PR, in-loop,
  delivery, blocks `done`) — but it does not seat a dedicated TESTER member; doing
  so dispatches into a wall (proven by `test_tier1_concurrent_fanout_completes`).
- **The over-planning / non-convergence of the PM** — the open finding above.

## How to reproduce

Dev-venv sidecar (probe needs the repo tree + `node_modules` + Chromium), flags
`dev_repo_read` / `reviewer_repo_read` / `web_probe` on, `max_parallel_workers`
unset. Poll read-only from `~/.errorta/council/coding-projects/gravity-golf`. The
delivered code lands in `~/.errorta/council/apply-workspaces/coding-gravity-golf`;
serve it over HTTP (ES modules need it) and drive `scripts/web-probe.mjs` for
out-of-band ground truth.
