# Spec 30 — An execution gate that runs the artifact, and grounded per-PR review

**Source:** Run 4 (`gravity-golf`, 2026-07-30) and the hand-merge that followed
it. Once [SPEC-29](SPEC-29-review-dispatch-and-merge-liveness.md) unstranded the
six module PRs, the merged tree *still* did not work: it crashed on the first
shot (`well.applyGravity is not a function`), and before that fix the original
delivery was a **big square** that renders but ignores input. Neither failure is
visible in a diff, and neither was caught by the council — because no role, and
no gate, ever *ran the assembled artifact and drove it*.
**Target version:** v0.1 (engine — `scripts/web-probe.mjs`,
`python/errorta_council/coding/web_probe.py`, `runner.py`)
**Depends on:** [SPEC-29](SPEC-29-review-dispatch-and-merge-liveness.md) (PRs must
be able to merge before an execution gate on the integrated tree means anything)
**Relates to:** GL01 (the black-canvas probe this spec upgrades) · known-open #1
(the structurally empty TESTER seat) · known-open #2/#3 (the ungrounded reviewer)
**Status:** landed
**Owner:** wiggins-j

---

## Problem

The council shipped software that does not run, twice, in one deliverable:

| Defect (in the merged game) | How it fails | Diff-visible? | Old probe caught it? |
|---|---|---|---|
| `hole.contains(x,y,r)` vs `contains(ball)` | wrong result | no | no |
| two incompatible `InputHandler` contracts | no input / crash | no | no |
| `ball.isStopped()` missing | crash after a shot | no | no |
| `well.applyGravity()` vs `applyTo()` | **crash on first shot** | no | no |
| empty `update()` (the "big square") | renders, ignores input | no | no |

Every one is a **runtime** fault. The DEV writes a module blind; the REVIEWER
reads a diff excerpt; the TESTER never arms (known-open #1); nobody loads the
integrated page and drives it. The failure report's Pathology 1 exactly: the
write→run→observe→fix loop was amputated, so contract mismatches that live *in the
seams between modules* — where no single agent looks — reach delivery intact.

Two engine facts made it inevitable:

1. **The probe only loaded, never interacted.** `web-probe.mjs` opened the page,
   waited N frames, and checked the first canvas was non-black. All four crashes
   fire only when the artifact is *driven*, so a passive load never triggered
   them; and the empty gradient is non-black, so it passed. The oracle GL01 built
   to catch run 1's black screen was both **gameable** (`fillRect` with the
   literal comment *"to prove canvas works"*) and **blind to interaction**.

2. **The completion gate excluded the probe.** `delivery_review` computed
   `passed = approved and tests_passed and launched_clean`. `approved` is the
   ungrounded LLM reviewer; `tests_passed` is vacuously true when no test was
   authored (the buildless-web case); `launched_clean` only means the server
   served HTTP 200. The web probe recorded a verdict here and delivery **ignored
   it** ("never blocks done"). So an empty gradient — server up, HTTP 200, non-
   black — shipped `done`.

## Principle

> An artifact is not done because it renders. It is done because it **runs**:
> loads without a console/page error, and **responds when driven**. The gate must
> execute the integrated artifact and refuse to ship when it crashes or is inert
> — and the reviewer must see that runtime evidence on the PR it is approving,
> not a diff alone.

## What landed

### S1 — the probe drives the artifact (`web-probe.mjs`)

After the passive frame-wait the probe now hashes the canvas, drives a realistic
pointer gesture across it (press–drag–release, plus a click — a "shot" for a mouse
game, a generic poke otherwise), lets it settle, and re-hashes. It emits two new
fields:

- `interaction_error` — a new `pageerror`/`console.error` fired *during* the
  gesture. This is what catches the crash-on-shot class: the drag triggers the
  `applyGravity`/`isStopped`/`InputHandler` faults, and the existing error path
  turns the run red.
- `interaction_changed` — did the canvas change after the gesture. `false` is the
  inert "big square": renders, ignores input.

Verified against three ground-truth builds of the delivered game: the crash
version fails on `interaction_error`; the empty gradient fails on
`interaction_changed=false`; the fixed game passes. The old probe passed all
three.

### S2 — the completion gate blocks on a red probe (`runner.py` `delivery_review`)

`passed = approved and tests_passed and launched_clean and probe_ok`. `probe_ok`
is false only when the probe **ran and came back red** (black, crash, or inert);
a probe that could not run (no browser) returns `None` and never blocks
(fail-open, unchanged). A red probe files a `fix web artifact runtime behavior`
DEV task carrying the probe's **verbatim** reason (the console crash line, or
"did not respond to input"), so the run re-opens and the team fixes it. This is
the change that stops the big square and the crash-on-shot from shipping.

Escape hatch: gated on the existing `web_probe` policy flag — `web_probe=false`
disables the probe entirely, restoring pre-SPEC-30 behaviour exactly.

### S4 — per-PR, pre-merge probe evidence for the reviewer (`runner.py`, `web_probe.py`)

The post-merge probe ran at the *master* head, which no open PR ever carries — so
its verdict never reached a PR the reviewer reads. `run_and_record` gained a
`serve_root` override; a new `_web_probe_pr_arm` runs at **PR-open**, serving the
PR branch's own worktree and recording the verdict bound to the **PR's head**, so
`_attach_verdict_to_prs` stamps that PR (`probe_passed` / `probe_reason` / …). The
reviewer then reads the verdict off the record and — via the existing
`latest_gate_text` segment (the most recent test-run, which is now this PR's
probe) — sees it verbatim in its prompt. The 26–92% false-rejection seat is no
longer ungrounded: it reviews the diff *and* the runtime behaviour of the exact
tree.

### S3 — known-open #1 resolved at the engine level, not by seating a member

Known-open #1 was framed as "no engine path registers a unit-scoped test command,
so a headless run can never arm its own TESTER — the seat is structurally empty."
The intent behind it was **execution grounding in the loop**. S1/S2/S4 provide
exactly that: the web:probe now runs per-PR, in-loop after each merge, and at
delivery, and a red verdict blocks `done`. The execution loop is closed.

Seating a TESTER *member* to re-run the probe was tried and rejected: granting the
tester `can_execute` on a web project makes closure report it `capable` → seated →
the concurrent loop dispatches it, and with no unit command its turn hits
`_changes_requested` and files a spurious "fix tests" task — the empty-seat
problem relabeled (caught by `test_tier1_concurrent_fanout_completes`). The
web:probe is an **engine** capability, not a seated-member one. The tester seat
therefore stays honestly `deferred` on a headless web run; its verification duty
is discharged by the probe. A dedicated tester turn that *runs* the probe as a
unit command (rather than the engine arms doing so) remains a possible future
refactor, but it would duplicate the arms, not add grounding.

## Regression locks

1. `web_probe=false` reproduces pre-SPEC-30 behaviour exactly (probe never runs,
   never blocks, no per-PR arm).
2. A probe that cannot run (no Playwright/Chromium) returns `None` and never
   blocks `done` — fail-open, unchanged. The merge gate (no browser in CI) is
   unaffected; the probe tiers stay `skipif`-gated.
3. A scripted probe verdict without the new fields (`interaction_changed` absent →
   `None`) is treated as "could not determine" and does not block — every
   pre-SPEC-30 test that scripts a green verdict stays green.
4. The reviewer prompt gains no new segment; grounding rides the existing
   `latest_gate_text` seam, so the prompt goldens are byte-identical.
5. The oracle must actually run in tests: the probe tiers pin
   `PLAYWRIGHT_BROWSERS_PATH` / snapshot the real env so the coding conftest's
   hermetic `HOME` cannot hide the browser and silently skip the check.

## Worst-case cost

One headless probe per PR-open (bounded by `_NODE_TIMEOUT_S`), plus the existing
in-loop and delivery probes — no new model calls. A non-web project skips every
arm (no profile). `web_probe=false` removes all of it.

## Definition of done

- The delivered artifact loads with **zero** console/page errors AND its canvas
  **changes when driven** — asserted by the interactive probe, enforced by the
  completion gate.
- A crash-on-interaction or an inert canvas blocks `done` and files a grounded
  DEV fix task; a run cannot reach `definition_of_done` on a non-running artifact.
- Each PR carries its own pre-merge runtime verdict, visible to the reviewer.
- Tier 1b (real Chromium drives the delivered artifact to `definition_of_done`)
  is green; the three ground-truth builds (crash / inert / working) are
  discriminated correctly.
