# Run Analysis — Gravity Golf (2026-07-24)

Analysis of the live `gravity-golf` coding run (started 17:16 UTC, PID 81711,
`claude_cli.opus` on all 6 members), combining the run's own ledgers with
**out-of-band ground truth**: I executed the acceptance gate myself and loaded
the assembled game in a real browser — things no role inside the run can do.

## TL;DR

The run is healthy by every counter the harness tracks (0 `member_failed`, 0
wedged-dispatchable, clean merges, aligned cross-module contracts) and is still
producing garbage-in-motion: it has spent its last ~20 minutes in a
single-dev revise spiral on a task that **no member is capable of performing**
("run the acceptance test"), rubber-stamp-reviewed by a reviewer that **cannot
see the repo**, serialized to one worker by a foundation gate waiting for a
`package.json` that this buildless project **should never need** — while the
actual game sits un-run, with a black-screen init race, two trivial levels, and
a self-sabotaging test harness that nobody in the loop can discover because
**nobody in the loop can execute anything**.

That last clause is the whole story, and it answers the operator's question
(§6): a one-shot Opus 4.8 in Claude Code beats this pipeline not because
multi-agent is a bad idea, but because the single agent has the one thing this
pipeline amputated — the write → **run** → observe → fix loop. Roles multiply
coordination; only execution multiplies quality.

---

## 1. Run snapshot

| Item | Value |
|---|---|
| Status | `running`, started 2026-07-24 17:16 UTC, no errors, `can_resume: false` |
| Members | pm-1, dev-1..3, reviewer-1, tester-1 — all `claude_cli.opus` |
| Turns (first 30 min) | 30: 13 `pr_opened`, 13 `pr_reviewed`, 3 `planned`, 1 `noop` |
| `member_failed` | **0** — the `claude_cli_empty_result` crash from the prior run is confirmed fixed in practice |
| Backlog | 27 distinct tasks, 26 done / 1 doing, no duplicate spiral (Spec 08 dedupe holding) |
| PRs | 14: 10 merged, 2 `changes_requested`, 1 superseded, 1 open |
| tester-1 turns | **0** (see §4.2) |
| `foundation_status` | **`pending`** — never lifted (see §4.2) |
| Delivery | nothing accepted; delivery root empty |

Positives worth keeping: task decomposition was clean (7 module tasks + wiring +
gate task, one file each), merges were conflict-free, and the cross-module
contract check passes — every `window.X` consumed anywhere is registered by
exactly one module, script order in `index.html` is correct with `main.js`
last. The `window.Audio`-vs-`window.AudioModule` bug class that killed the last
two runs did not recur. Spec 11 repo-read plausibly deserves credit; see §5.3
for the caveat.

Note on the 20K-char prompts in `turns.jsonl`: that is `_TURN_FIELD_CAP =
20_000` in `coding/ledger.py` — an **audit-copy cap only**. Live prompts are not
truncated. Observability gap, not a context-loss bug.

## 2. Ground truth: what the product actually is

I copied master out of the apply workspace, installed jsdom, ran the gate, and
loaded the game in a browser. Results:

### 2.1 The acceptance gate fails — but the game is fine

`node test/acceptance.test.js` fails at initialization: `getState()` returns
`ball: null, level: null, hole: null`.

Root cause is **in the test harness, not the game**. `createGameEnvironment()`
constructs JSDOM with `resources: 'usable'` on the real `index.html` (which
contains the seven `<script src>` tags) **and then manually injects the same
seven scripts inline** and dispatches a synthetic `DOMContentLoaded`. The
injected copies run and initialize correctly — then the async resource-loaded
copies of the `<script src>` tags re-execute every module, and a fresh,
never-initialized `window.GravityGolf` IIFE instance overwrites the initialized
one. Verified both directions: with `resources: 'usable'` removed (one-line
change), initialization passes and the gate runs to completion.

### 2.2 Gate results after the one-line fix: 10/12 levels pass

```
lev | par | strokes | status
  0 |   2 |       2 | PASS
  1 |   2 |       2 | PASS
  2 |   3 |       2 | PASS
  3 |   3 |       0 | FAIL   <- trivial: straight-line path to hole is clear
  4 |   3 |       2 | PASS
  5 |   3 |       0 | FAIL   <- trivial: straight-line path to hole is clear
  6 |   4 |       1 | PASS
  7 |   4 |       2 | PASS
  8 |   4 |       1 | PASS
  9 |   5 |       2 | PASS
 10 |   5 |       2 | PASS
 11 |   5 |       3 | PASS
```

Levels 3 and 5 violate the DoD's non-triviality requirement. The solver solves
every level within par+2. Physics is sound enough to complete the course.

### 2.3 Real browser: black screen on load

Served over HTTP and loaded in a browser: **zero console errors, fully
initialized game state — and a pure black screen.** `Render.init()` sizes the
canvas backing store from `getBoundingClientRect()` at `DOMContentLoaded`; in
the embedded browser that returned 0, so `canvas.width = 0` and every draw is a
no-op. There is no resize handler and no per-frame size guard to recover.
Calling `GravityGolf.init()` again by hand fixed it instantly and the game
rendered and played. This is the same *class* of failure as the prior run's
"green square": init-order/visibility bugs that only execution can surface.

### 2.4 Visual bar: prototype, not premium

Once rendering: flat green gradient course, simple beveled walls, ball with
basic shading, glowing hole. Missing vs the DoD: texture/lighting depth, motion
trail/spin, letterboxing (the 800×600 level renders anchored left in a 1280×720
canvas with a dead right third), polished HUD. Plus a string bug: HUD shows
"Level 1: Level" — the UI reads a field that doesn't exist; the level's actual
name is "First Steps". The reviewer approved "Premium visual rendering"
without ever seeing a pixel.

## 3. What the loop is doing right now: an impossible-task spiral

Timeline: after the 9 foundation tasks merged (17:18–17:28), the PM planned
"Run acceptance gate and fix failures". From then on:

1. A dev opens a PR that *edits code or the test file* (all it can do).
2. The reviewer rejects it: *"no evidence that the tests were actually run…
   no strokes-per-level table"* — correct by the task text, and **impossible to
   satisfy**: no role has an execute tool.
3. A `revise:` task is spawned. GOTO 1.

By 17:45 the chain is `revise: task-t-6ad32880fe5d` — a revise of a revise of a
revise — on ~2-minute cycles, dev-1 only, reviewer rejecting each round. Every
wedge detector shows green because tasks keep completing; the run will burn
iterations until a stall limit trips. This is a **capability wedge**: a
livelock the current `todo/dispatchable` wedge detection cannot see.

## 4. Root causes (harness)

### 4.1 Nobody can execute anything (P0)

`turn_controller.py` `_ROLE_TOOLS`: DEV = `("code_write",)`; PM, REVIEWER,
TESTER = `()`. The F087-14 comment is honest about why (previously advertised
`code_exec` had no executor — over-promising), but the consequence is that a
DoD written as *"iterate until the gate passes"* is structurally unsatisfiable:
the gate cannot run inside the loop, so "iterate until" has no feedback signal.
Every defect in §2 — the harness self-sabotage, the black screen, the trivial
levels — is discoverable in seconds by running the artifact and in principle
never by re-reading it.

### 4.2 Foundation-gate deadlock → serialization + dead tester (P0)

`runner.py` F139/F142: a greenfield project with `.js` source
(`_MANIFEST_BOUND_EXT` includes `.js`) is foundation-ready only when a build
manifest is **merged to master**. Until then worker concurrency clamps to 1.

- This project is **deliberately buildless** ("opens directly in a browser with
  no build step"). Its foundation *is* `index.html` + script tags. No manifest
  will ever be legitimately required; the only reason `package.json` exists at
  all is that the dev-authored test wants jsdom.
- The `package.json` PR (t-99a011c7bc6e) was rejected for an unrelated reason
  (the impossible "run the test" scope), so `foundation_status` is `pending`
  forever, the 3-dev fan-out the operator believed happened never did — turn
  timestamps show strictly serial execution end to end — and whatever runtime
  setup gates tester dispatch never initialized: **tester-1 has 0 turns** in a
  run whose DoD is one giant test loop.

### 4.3 Review is theater (P0)

11 of 13 reviews: 5.5–8.7s wall time, `approved: true, findings: []`,
byte-identical shape. The reviewer has no tools and only sees a (truncatable)
diff in its prompt; it approved every foundation module including the rendering
it was explicitly told to hold to a visual bar. When it finally did reject, it
demanded execution evidence nobody can produce (§3). One review turn
(t-690c64b3c890, 40.5s) shows the model attempting Claude-Code-style
`<function_calls>` Read/Glob invocations — **leaked as literal XML text** into
the response, because the reviewer's CLI invocation exposes no tools. The model
knows what it needs (to read the repo); the harness won't let it.

### 4.4 Prompt/tool-catalog contradiction burned a turn and blinded the wiring task (P1)

The work request tells devs *"You can read any file in the repo"*. The errorta
tool catalog says *"Available tools: code_write"*. The actual Spec 11 mechanism
(CLI invoked with cwd = worktree + native read-only tools + turn budget,
`async_claude_cli.py`) is invisible in that framing. Result: dev-1 emitted a
tool-plan for a hallucinated `read_files` errorta tool → `tool_not_allowed` →
`noop` turn → then wrote `main.js` (the integration-critical file) **blind** on
the retry. It got the contract right anyway — this time.

### 4.5 No circuit breaker on revise chains (P1)

Same-finding rejections recur ≥4 deep with no escalation to the PM, no
reclassification, no "this finding requires a capability no role has" check.
`task_reassignment_limit: 2` doesn't apply because each revise is a *new* task.

## 5. The operator's question: why does this lose to a one-shot Opus 4.8 in Claude Code?

Because today the council is **N narrower copies of the same model, minus the
feedback loop, plus coordination overhead** — and the premise of role-based
loop engineering ("old-school software team") is only an advantage when each
role adds *independent verification*, which none currently can.

Concretely, a single Claude Code session with this prompt gets: full repo
read/write, Bash (run node, run the test, see the strokes table), a browser
(see the black screen immediately), and tight iteration — the model observes
reality dozens of times before finishing. In this run:

- The **dev** is Opus with one write tool, a bounded read budget, and no
  execution. It ships plausible code it can never test. (One-shot Opus would
  have hit the canvas-0×0 bug on its first manual check.)
- The **reviewer** is Opus with *no* tools judging a diff excerpt. Its
  approvals carry no information (6-second empty verdicts); its rejections
  demand the impossible. In a one-shot there is no reviewer, and nothing of
  value was lost — a reviewer that can't ground its findings is pure latency.
- The **tester** — the one role that *would* out-verify a one-shot — never ran.
- The promised **parallelism** was clamped to 1 the whole run (§4.2), so the
  pipeline paid multi-agent token overhead (per-turn CLI spawns, PM turns,
  review turns, prompt reassembly — 30 turns for what a one-shot does in one
  session) at zero concurrency benefit.

So the current infrastructure converts one strong agent into several weak ones
and spends the token budget on process artifacts (PRs, verdicts, revise chains)
instead of verification. **This is an infrastructure gap, not a concept
failure.** The design intent is already correct in places — the F087-14 comment
says the tester verdict should be "derived from a real, grounded test run" —
it's just not wired. The pipeline out-performs a one-shot only when: the gate
actually executes inside the loop (quality floor a one-shot must self-impose),
the reviewer independently *reproduces* claims rather than reading diffs
(catches what the author-model is systematically blind to), and devs truly
parallelize (throughput). All three are buildable; §6 is the list. Until S1
and S3 land, expect one-shot Claude Code to win on quality-per-token, and
prefer it for anything single-session-sized.

## 6. Specs / fixes needed

### Harness (errorta_app)

- **S1 (P0) — Execute the gate inside the loop.** Give tester-1 a real runtime:
  run the project's acceptance command (sandboxed node / headless browser) after
  every merge that touches gate-relevant files; append the **verbatim**
  output (exit code, strokes table, stack traces) to subsequent dev and
  reviewer prompts; make `project_done` require a green in-loop gate run. This
  single spec converts every downstream failure in this run. (Builds on the
  existing runner-runtime/`_has_runnable_runtime` scaffolding and Spec 11
  verbatim-failure propagation.)
- **S2 (P0) — Foundation gate: recognize buildless web projects.** An
  `index.html` on master whose `<script src>` graph resolves against files on
  master is a complete foundation for a no-build web target; do not require a
  JS manifest (F142 WS-B treated `.js` as always manifest-bound). Also:
  foundation-unlocking PRs should be fast-tracked or PM-flagged so an unrelated
  rejection can't hold the concurrency clamp and runtime setup hostage forever.
- **S3 (P0) — Ground the reviewer.** Invoke reviewer turns with the same
  read-only CLI mechanism devs get (cwd = merged workspace, Read/Grep/Glob,
  bounded turns) so its tool hunger (§4.3) becomes capability instead of XML
  leakage; require findings/approvals to cite file:line evidence; treat a
  sub-floor-latency empty verdict as unparsed and retry. Once S1 exists, give
  the reviewer the latest gate output in-prompt. For visual DoDs, attach a
  headless screenshot artifact to the review prompt (S6-lite).
- **S4 (P1) — Capability-aware planning and finding routing.** The PM prompt
  should enumerate role capabilities; lint task/finding text so "run/execute/
  measure/verify-by-running" imperatives are routed to the tester (post-S1) or
  rejected at planning time, never assigned to a write-only dev.
- **S5 (P1) — Revise-chain circuit breaker.** N (=2–3) consecutive rejections
  of the same finding class on one lineage → stop spawning `revise:` tasks,
  escalate to a PM re-plan turn with the finding attached; count revise
  lineages toward wedge detection (the current detector misses this livelock
  entirely, §3).
- **S6 (P2) — Prompt/tool-catalog coherence.** The work-request text, the tool
  catalog, and the actual CLI-native tool surface must describe the same
  reality (§4.4); the catalog line should name the CLI-native read tools when
  repo-read is active.
- **S7 (P2) — CLI UX.** `errorta status` from an unbound directory should list
  active projects/runs and how to target one, rather than just "(none bound)".

### Product (gravity-golf, if salvaging this run's output)

All four are small:

1. `test/acceptance.test.js`: remove `resources: 'usable'` (or strip
   `<script src>` tags from the HTML before JSDOM) — un-sabotages the gate.
2. `src/render.js` / `main.js`: guard init against zero-size layout (retry on
   first rAF) and add a resize handler — fixes the real-browser black screen.
   Also guard `init` on `document.readyState` for late-load robustness.
3. `src/levels.js`: regenerate levels 3 and 5 so the straight-line path is
   blocked (gate's own triviality check then passes 12/12).
4. `src/ui.js`: HUD reads a nonexistent level-name field ("Level 1: Level");
   use `level.name`. Letterbox the 800×600 course into the canvas.
