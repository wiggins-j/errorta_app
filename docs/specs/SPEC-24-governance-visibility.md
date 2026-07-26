# Spec 24 — Governance visibility (the PM can see the countdown)

**Source:** [`ROADMAP-autonomy.md`](ROADMAP-autonomy.md) Phase 2, gap **G2** —
"the harness judges without showing evidence"; and
[SPEC-23](SPEC-23-continue-by-default.md), which closes G2's *authority* half and
explicitly defers this one (SPEC-23 "Out of scope", first bullet).
**Target version:** v0.1 (engine — `errorta_council/coding/`)
**Status:** proposed
**Owner:** wiggins-j

---

## Problem

> You cannot course-correct against a threshold you cannot observe.

The PM plans every batch of work in this system. It is the same model, in the
same room, that diagnosed the 2026-07-26 stops from the outside once an operator
pasted the trace in. The difference between the two readings is not intelligence;
it is **information**. The operator could see that the gate had been flat for six
iterations. The PM could not, because nothing in its prompt says so.

Concretely, `_pm_prompt_segments` (`runner.py:2338-2396`) assembles seven
segments: the Current Focus pin, the role head + done-gate, the F129 model
catalog, the orientation packet, the boot/grounding packet, the Spec 15
capability block, and the standing planning instructions. Not one of them carries
a single number produced by the loop. Meanwhile, between those turns, twelve
`_account_*` detectors read the ledger, compute a reading, compare it to a
threshold, and decide whether the run lives — for example
`_account_gate_stall` (`autonomy.py:1072-1118`) holds `score`,
`c.last_gate_best`, and `c.iterations - c.last_gate_iter` in local variables and
in `LoopCounters`, trips at `gate_stall_limit` (default **8**,
`autonomy.py:126`), and its evidence string —

> `f"acceptance gate has not improved for {policy.gate_stall_limit} iterations (score={score})"`
> (`autonomy.py:1116-1117`)

— is handed to `_maybe_raise_monitor` (`autonomy.py:916`) and **thrown at a human**
one line before the run ends. The PM, whose next plan turn is the only thing that
could have changed the outcome, is told nothing, ever.

The same holds for every window: `c.pm_idle` against `pm_idle_limit=2`
(`autonomy.py:71`, tripped at `:1885` / `:2272`); `c.plan_streak` against
`plan_streak_limit=6` (`:134`, `_account_planning_churn:1327`); `c.wedge_streak`
against `wedge_stall_limit=5` (`:145`, `_account_dispatch_wedge:1417`);
`c.iterations - c.last_broken_iter` against `revise_livelock_limit=5` (`:199`,
`_account_revise_livelock:1256`); `c.delivery_review_rounds` against
`delivery_review_round_limit=3` (`:110`, tripped at `:1837` / `:2210`);
`c.iterations` against `max_iterations=200` (`:67`, `:1815`). Twelve countdowns,
zero of them observable by the one component that could act.

And the softest of them is the sharpest example. GL04's convergence clamp
(`_account_convergence_clamp`, `autonomy.py:1150-1200`) does not stop the run —
it **changes how the run behaves**, forcing serial integration by setting
`convergence_clamped`, which `runtime_cap` reads at `autonomy.py:569-570`. So the
PM's fan-out silently collapses to one worker, its plan batches stop being
dispatched in parallel, and there is no statement anywhere in its prompt that
this happened or why (superseded-ratio over the last `convergence_window=20`
resolved PRs crossed `convergence_clamp_ratio=0.5`). A PM planning a wide batch
into a clamped run is planning against a machine it cannot see.

## Why the existing machinery didn't catch it

Every part needed for this already exists, pointed in every direction except this
one.

- **The segment mechanism is built and proven.** `PromptSegment` /
  `join_segments` (`runner.py:153-168`) plus the composition accounting
  (`_composition_from_segments:171`, `_register_pending_composition:198`) make an
  added prompt block a one-line, token-attributed, golden-lockable change. Spec 12
  used it for `gate_output` (`runner.py:2709`, `:2889`, `:3256`) and Spec 15/17
  for `tool_guidance` (`:2382`, `:2392`). Nobody pointed it at the loop.
- **The cross-module state seam is built and proven.** `run_state` is one locked
  read-modify-write JSON doc (`ledger.py:1469-1498`), and it is already how the
  engine talks to itself across module boundaries: `_account_convergence_clamp`
  writes `convergence_clamped` (`autonomy.py:1210`) for `runtime_cap`
  (`:569`) to read; the runner writes `foundation_status` (`runner.py:3698`) and
  `_progress_fingerprint` reads it back (`autonomy.py:616`); `gate_due`
  (`runner.py:4002`), `frozen_paths` (`:4158`), `contract_owner_task_id` (`:4091`)
  all travel the same way. Detector windows never joined them.
- **The counters *are* persisted — once, at the end, when they can no longer
  change anything.** The terminal `set_run_state(counters=…)` writes five fields
  (`runner.py:6368-6376`, mirrored in `routes/coding.py:2536-2544`) *after* the
  loop returns. That is a post-mortem, not telemetry.
- **The detectors' readings die in their own stack frames.** Each `_account_*` is
  `-> Optional[LoopResult]`, computes its reading locally, and has exactly one
  export path: the reason string it hands `_maybe_raise_monitor`. That function
  returns early for ungoverned runs (`autonomy.py:929-930`) and writes an
  attention signal — a **human** surface. `attention.list_open` is never read by
  any prompt builder (the only in-runner use is the GL05 audit dedupe,
  `runner.py:2216`).
- **And the seam between the loop and prompt assembly is two arguments wide.**
  `RunTurn = Callable[[Any, Any], TurnOutcome]` (`autonomy.py:837`): the loop
  hands a turn `(action, ledger)`. `LoopCounters` is not among them, and cannot
  simply be added — `build_run_turn` (`runner.py:4170`) constructs the closure
  **once at run start** (`runner.py:6342-6348`), before the loop exists, and the
  concurrent loop shares that one closure across a `ThreadPoolExecutor`
  (documented hazard, `runner.py:4202-4208`).

So: a prompt mechanism with nothing to say, a state seam nobody routed through,
and readings that only ever escape to a human at the moment of death. This spec
connects the three.

## Goals

- Every reading a detector is about to act on is **observable by the PM in the
  turn before it acts**, with its current value, its threshold, and how close it
  is — for `no_progress`, `not_converging`, `gate_not_improving`,
  `planning_churn`, `dispatch_wedged`, `revise_livelock`,
  `delivery_review_stalled`, the F127 unproductive ladder, the F128 false-done
  streak, F120 member health, the GL04 convergence clamp, and the run budget.
- **One** computation of each reading, shared by the detector, the prompt, and
  SPEC-23's last-word turn. No second implementation of "how close is the gate
  stall" anywhere in the tree.
- **Silent when it has nothing to say.** A run with nothing near a threshold
  produces a byte-identical prompt to today's, so
  `test_prompt_segments_golden.py` stays green unmodified.
- Framed unambiguously as **observed state**, never as instruction and never as a
  threat — a PM must not read a countdown as a hint to declare victory.
- A **drift lock**: a detector added later cannot silently become invisible.

## Non-goals

- **Not a dashboard or a CLI surface.** `errorta status`
  (`python/errorta_cli/commands/status.py`) is the operator's view and already
  reads `run_state`; the snapshot this spec publishes lands there for free but
  rendering it is a follow-up, not this spec. This spec ships exactly one
  consumer: the PM prompt.
- **Not new detectors, and not retuning a single threshold.** Same prohibition
  the roadmap states ("Explicitly not in scope") and SPEC-23 repeats. This spec
  changes what the PM can *see*; SPEC-27 changes what a threshold *does*.
- **Not letting the PM disable, extend, or argue with a detector.** There is no
  turn shape here by which the PM can raise a limit, reset a window, or suppress
  a reading. Reading is not negotiation — negotiation is SPEC-25, and the one
  legitimate override channel is SPEC-23's last-word turn, which is bounded and
  already specified.
- **Not rendering into DEV / reviewer / tester prompts.** Run-level governance is
  a PM concern; a dev turn cannot act on `plan_streak` and would only pay tokens
  for it.
- **Not touching the governance design-phase PM prompt**
  (`build_pm_governance_prompt`, dispatched at `runner.py:4381`). Different
  envelope, different phase, no detector windows running yet.
- **Not persisting `LoopCounters` across resume.** SPEC-23 already names that
  finding and persists exactly one field for its own bound; this spec inherits
  the re-arming behaviour and only makes it *legible* (see Edge cases).

---

## Item 1 — The seam: a published detector snapshot on `run_state`

**This is the load-bearing decision.** The state the PM needs is split: some of it
is per-process window state that exists **only** in `LoopCounters` (`pm_idle`
`:754`, `plan_streak` `:821`, `wedge_streak` `:827`, `last_progress_iter` `:786`,
`last_gate_iter`/`last_gate_best` `:797-798`, `last_broken_iter` `:807`,
`delivery_review_rounds` `:811`, `unproductive_counts` `:761`,
`member_fail_counts` `:758`, `false_done_streak` `:769`, `iterations`/
`model_calls` `:750-751`), and some of it is ledger-derived (the gate score, the
backlog shape, the wedge culprits, the superseded ratio). The prompt is composed
in `runner.py`, which has the ledger and **does not have `LoopCounters`**.

**Design.** Each iteration, at the point where the detectors already compute, the
loop **publishes a compact JSON snapshot** into `run_state` under one key,
`detector_state`. The prompt builder reads it back from the store it already
holds.

```python
# python/errorta_council/coding/detector_state.py  (new; mirrors gate_state.py)
def publish(ledger, counters, policy, *, focus: str | None = None) -> None
def snapshot(ledger) -> dict | None
def prompt_text(ledger, *, focus: str | None = None) -> str   # "" == say nothing
```

`publish` is called from **both** loop chains, immediately after the detector
chain runs at the quiescent point: `_run_sequential_loop` after the
`_account_dispatch_wedge` block (`autonomy.py:1921-1923`, before the checkpoint
check at `:1926`), and `_run_concurrent_loop` at the identical position
(`:2306-2308`, before `:2309`). Same placement, same inputs, same reason SPEC-23
Item 5 gives: *a hook in only one chain is dead code exactly where it is needed*
(the comment already in the tree at `autonomy.py:2298-2301`).

Parameters are duck-typed (`Any`), so `detector_state.py` imports neither
`autonomy` nor `runner` — the `gate_state.py` / `paths.py` circular-import
discipline (`gate_state.py:21-24`), which is what lets `autonomy` call `publish`
and `runner` call `prompt_text` with no cycle.

**Δ note — why not thread `LoopCounters` through `build_run_turn`.** Three
reasons, any one sufficient. (1) The seam is typed `Callable[[Any, Any],
TurnOutcome]` (`autonomy.py:837`) and the factory has ~50 direct test callers;
widening both is a large diff for a read-only feature. (2) It would not work:
`build_run_turn` is invoked once at run start (`runner.py:6342-6348`), *before*
`run_coding_loop` creates or receives the counters (`autonomy.py:900`), so the
closure could only capture a mutable reference. (3) That reference would then be
read from pool worker threads while the main thread mutates it in the apply phase
(`autonomy.py:2196-2205`) — the exact class of hazard the per-turn capture
scratch was made thread-local to avoid (`runner.py:4202-4208`). The published
snapshot is an immutable value written at a quiescent point; it cannot race.

**Δ note — why not recompute the readings in `runner` at prompt time.** Half of
them have **no ledger representation at all**. `pm_idle`, `plan_streak`,
`wedge_streak` and the `last_*_iter` marks are pure in-memory windows; there is
nothing to recompute them from. Recomputing the ledger-derived half and guessing
the rest would put a second, partial implementation of every threshold next to
the first — the "four declarations, four values" shape [SPEC-19](SPEC-19-version-identity-and-build-provenance.md)
exists to prevent, with the same failure mode: the prompt says 4-of-8 while
`_account_gate_stall` says 6-of-8, and the PM course-corrects against a number
that is not the one being enforced.

**Δ note — one new extraction, to keep the single-computation goal true.** The
snapshot reuses `_gate_fingerprint` (`autonomy.py:622`), `_gate_has_failure`
(`:1121`), `_open_backlog_shape` (`:1303`) and `_dispatch_wedge_culprits`
(`:1356`) as-is. The GL04 window arithmetic is the only reading currently inlined
in its detector (`autonomy.py:1177-1187`); it is extracted to
`_convergence_window_stats(ledger, policy) -> tuple[float, float, int] | None`
and `_account_convergence_clamp` is rewritten to call it. Behaviour-identical,
and it is the difference between one implementation and two.

**Δ note — write cost.** `set_run_state` is a locked read-modify-write of one
small JSON doc (`ledger.py:1490-1498`); the loop already performs several per
iteration. `publish` additionally **compares against the currently-stored
snapshot and writes only on change**, so a quiet run — where the snapshot is
`None` iteration after iteration — performs zero writes after the first.

**Acceptance.** After an iteration, `ledger.get_run_state()["detector_state"]`
carries the readings the detectors just computed, and `prompt_text` reproduces
them. `publish` is called from both loop bodies (grep lock, Testing). A ledger
failure inside `publish` leaves the run unaffected (it is wrapped exactly like
`_account_convergence_clamp`'s writes, `autonomy.py:1209-1212`). No public
signature in `autonomy.py` or `runner.py` changes.

## Item 2 — What the snapshot carries

**Design.** One row per governed window. `source` is where the number comes from,
and is the answer to "which detectors, read from where".

| Reading | Threshold (default) | Detector / trip site | Source |
|---|---|---|---|
| `pm_idle` | `pm_idle_limit` (2) | inline, `:1885` / `:2272` | counters `:754` |
| iterations since progress fp changed | `convergence_stall_limit` (20) | `_account_convergence:1048` | counters `:785-786` |
| gate score + iterations since strict improvement + *is anything failing* | `gate_stall_limit` (8) | `_account_gate_stall:1072` | counters `:796-798` + `_gate_fingerprint:622` + `_gate_has_failure:1121` |
| `plan_streak` + backlog shape `(open, distinct titles)` | `plan_streak_limit` (6) | `_account_planning_churn:1327` | counters `:821` + `_open_backlog_shape:1303` |
| `wedge_streak` + named culprit deps | `wedge_stall_limit` (5), floor `wedge_min_tasks` (10) | `_account_dispatch_wedge:1417` | counters `:827` + `_dispatch_wedge_culprits:1356` |
| open broken lineages + iterations since a merge | `revise_livelock_limit` (5) | `_account_revise_livelock:1256` | counters `:805-807` + `ledger.list_tasks`/`list_prs` (`:1277-1286`) |
| `delivery_review_rounds` | `delivery_review_round_limit` (3) | inline, `:1837` / `:2210` | counters `:811` |
| per-`(member, task)` unproductive count; reassignments / escalations / assists spent | `worker_unproductive_limit` (2), `model_escalation_limit` (2), `task_reassignment_limit` (2), `pm_assist_limit` (1) | `_handle_unproductive:1513` | counters `:761`, `:763-765` |
| `false_done_streak` | `completion_refused_limit` (2) | `_handle_completion_refused:1699` | counters `:769` |
| per-member consecutive call failures | `member_failure_limit` (3), classify-aware | `_account_member_outcome:1746` | counters `:758` |
| superseded-ratio + merge-rate over the resolved window; **clamp engaged?** | `convergence_clamp_ratio` (0.5) / `convergence_window` (20) | `_account_convergence_clamp:1150` | `_convergence_window_stats` (Item 1) + `run_state.convergence_clamped` |
| `iterations`, `model_calls` | `max_iterations` (200), `max_model_calls` (None) | `:1815-1818` / `:2052-2055` | counters `:750-751` |
| open attention signals (title, blocking) | — | — | `attention.list_open` (`attention.py:188`) |

**Deliberately not rendered**, recorded so a reader sees a decision rather than an
oversight:

| Not rendered | Why |
|---|---|
| `definition_of_done`, `no_actionable_work`, `cancelled`, `checkpoint`, `hard_blocker` | no window and no countdown — they are events, not approaches (`no_actionable_work` comes from `decide_next`, `:1811-1812` / `:2181-2186`) |
| `foundation_stall` (`:945`), `hot_freeze_stall` (`:1013`) | advisory-only, never a stop; both already raise an attention signal, which the segment's last line reports |

**Shape** (JSON-serializable; `run_state` is written by `_atomic_write_json`):

```json
{"iteration": 41, "model_calls": 96,
 "near": [{"detector": "gate_not_improving", "current": 5, "threshold": 8,
           "unit": "iterations", "reading": "score 6, gate has failing commands"}],
 "clamped": false,
 "budget": {"iterations": 41, "max_iterations": 200,
            "model_calls": 96, "max_model_calls": null},
 "signals": [{"title": "…", "blocking": true}],
 "last_word_available": true, "focus": null}
```

**Δ note — cheap fields first, expensive fields only when near.** Every counter
field is free (already in memory). The ledger-derived enrichments —
`_dispatch_wedge_culprits` in particular, which walks the whole `depends_on`
closure (`:1384-1401`) — are computed **only for readings that already passed the
Item 3 proximity test on their counter**. A quiet run therefore adds no ledger
reads at all.

**Acceptance.** For a run at `pm_idle=1, plan_streak=4, gate flat 5`, the
snapshot's `near` holds exactly those three entries with those values and their
live thresholds; `wedge_streak=0` produces no entry and no
`_dispatch_wedge_culprits` call.

## Item 3 — The proximity rule, and when the segment is absent

**Design.** One policy field:

```python
governance_proximity: float = 0.6   # 0.0 disables the whole segment
```

on `CodingAutonomyPolicy` (beside the other knobs, `autonomy.py:65-252`),
round-tripped in `policy_to_dict` (`:253-296`) and `policy_from_dict` (`:299`),
clamped exactly as `convergence_clamp_ratio` is (`:388-389`).

A reading is **near** when

```
trigger(threshold) = min(threshold - 1, max(1, ceil(ratio * threshold)))
near = trigger >= 1 and current >= trigger
```

The `threshold - 1` clamp is load-bearing, not defensive: without it a small
threshold has no warning band at all. `pm_idle_limit` is **2**, so
`ceil(0.6 × 2) = 2` — the PM would first be told about idleness in the same
iteration the run stops on it, which is worthless. Worked at defaults:

| Detector | Threshold | Trigger | First rendered when |
|---|---|---|---|
| `no_progress` | 2 | 1 | one idle PM turn has happened |
| `gate_not_improving` | 8 | 5 | gate flat 5 iterations |
| `planning_churn` | 6 | 4 | 4 plan turns, no worker turn |
| `dispatch_wedged` | 5 | 3 | wedged 3 iterations |
| `revise_livelock` | 5 | 3 | 3 iterations, broken lineage, no merge |
| `delivery_review_stalled` | 3 | 2 | 2 delivery rejections |
| `not_converging` | 20 | 12 | 12 iterations of no motion |
| `completion_refused` | 2 | 1 | one refused done-claim |
| `worker_unproductive` (per task) | 2 | 1 | one unusable turn on that task |
| `member_unhealthy` | 3 | 2 | 2 consecutive failures by one member |
| `budget_exhausted` | 200 | 120 | iteration 120 of 200 |

**The absence rule.** `publish` writes `detector_state = None` — and
`prompt_text` returns `""`, and `_pm_prompt_segments` **omits the segment
entirely** rather than emitting an empty one — when *all* of:

- no reading is near;
- the GL04 clamp is not engaged;
- there are no open attention signals.

This is verbatim the contract Spec 12 established for `gate_output`
(`gate_state.py:88-91`: *"Callers MUST omit their prompt segment entirely in that
case rather than emitting an empty one — that is what keeps a gate-less project's
prompts byte-identical to today (the goldens depend on it)"*), and it is why a
healthy run's PM prompt does not change by one byte.

**Two exceptions to "near", both stated so they are not mistaken for bugs.**
The GL04 clamp renders whenever it is **engaged** (it is a state, not a
countdown — the run is already behaving differently) or when the superseded ratio
has reached `governance_proximity × convergence_clamp_ratio` over a full window;
its `merge_rate` is reported alongside but never triggers on its own, because a
"lower is worse" floor does not compose with a fraction-of-threshold rule without
contrivance. And a `focus` render (Item 6) ignores proximity for the focused
detector, which has by then already tripped.

**Δ note — why one ratio and not per-detector bands.** Twelve tunables is twelve
more things to drift, and the roadmap forbids threshold tuning as the mode of
fixing this system. One ratio plus the `threshold - 1` clamp produces a sensible
band for every window at defaults (table above), and the operator who really
wants a detector quiet already has the honest lever: set that detector's own
limit.

**Acceptance.** `governance_proximity=0.0` reproduces today's prompt bytes for
every run, near or not (the kill switch). At the default, a run with every
counter below its trigger and no open signals produces no `detector_state` key,
no segment, and no ledger reads beyond the counters.

## Item 4 — The wording: observed state, not an instruction, not a threat

**Design.** `prompt_text` renders a bounded prose block (cap 1200 chars, the
`gate_state._PER_COMMAND_CAP` order of magnitude), one line per near reading:

```
GOVERNANCE STATE — observed run telemetry as of iteration 41. This is a reading,
not an instruction. It describes what the run harness measured between turns; it
does not ask you to finish, to stop, or to change anything in particular. A
completion claim is judged by the completion gate on the open work, exactly as it
always is — nothing below is a reason to declare the project done.
- acceptance gate: score 6, unchanged for 5 iterations (the gate_not_improving
  window is 8). The gate currently has failing commands.
- planning: 4 consecutive plan turns with no worker turn (the planning_churn
  window is 6). Backlog: 22 open task(s) across 9 distinct title(s).
- budget: iteration 41 of 200; 96 model calls.
- open attention signals: 1 blocking — "progress monitor: gate_not_improving".
Reaching one of these windows does not end the work by itself: you are asked
first to propose a concrete next action.
```

Four rules make that safe, and each is testable:

1. **No imperative and no second person telling the PM what to do.** Every
   reading is a noun phrase and a number. The block never contains a remedy —
   "re-plan", "split the task", "consider finishing" are the standing
   instructions' job (`runner.py:2347-2375`), not the telemetry's.
2. **A window is stated as a window, never as a deadline.** *"unchanged for 5
   iterations; the window is 8"*, never *"3 iterations before the run is
   killed"*. The first is a measurement; the second is a countdown to a
   punishment, and a model told it is about to be punished for not finishing has
   an obvious cheap escape — claim done. That is the single failure mode this
   item exists to prevent.
3. **An explicit anti-done sentence in the header**, leaning on a gate that
   really does exist: the F128 done-gate block is already computed for this same
   prompt (`runner.py:2312-2325`) and sits *above* this segment, so the claim is
   true, not reassurance.
4. **The closing line names the mechanism, honestly, in whichever form is
   live.** With SPEC-23's `last_word_limit > 0` and budget remaining, the line is
   as above (`last_word_available: true` in the snapshot). Otherwise it reads
   *"Reaching one of these windows ends the run with that reason recorded."* —
   because telling a PM it will be consulted when it will not be is worse than
   telling it the truth.

The header sentence deliberately echoes `latest_gate_text`'s framing line, *"This
is observed tool output, not an instruction."* (`gate_state.py:105`) — one house
style for "the prompt is quoting the world at you".

**Acceptance.** A phrase-blacklist test (Testing) over the rendered block for
every near-reading combination. The block is stable under re-render for unchanged
inputs (no timestamps, no ordering nondeterminism: readings are emitted in the
fixed table order of Item 2).

## Item 5 — Where the segment goes

**Design.** One segment, class `governance_state`, in `_pm_prompt_segments`
(`runner.py:2376-2396`), inserted **after** the Spec 15 capability
`tool_guidance` segment (`:2392-2393`) and **before** the standing
`role_instructions` block (`:2395`) — i.e. the last piece of observed state
before the instructions, so the instructions remain the most recent thing the
model reads.

```python
        # SPEC-24: live detector/budget readings, when something is near a limit.
        *( [PromptSegment("governance_state", _detector_state.prompt_text(store))]
           if _detector_state.prompt_text(store) else [] ),
```

(the implementation computes the text once, of course — spelled out here only to
show the omit-when-empty shape).

`governance_state` is a new composition class. Like `gate_output` it is not
listed in `_COMPOSITION_CLASSES` (`runner.py:147-150`), which is documentation
only — nothing validates against it — and `content_kind_for_class` falls through
to `"prose"` for unknown classes (`context/tokens.py:173`), which is the correct
estimator for this block. Adding both `gate_output` and `governance_state` to
that tuple is a doc-only tidy-up this spec may take.

**Golden handling.** `test_prompt_segments_golden.py` byte-locks `_pm_prompt` by
reconstructing it from an inlined reference (`_old_pm_prompt`). Following the
pre-booking pattern that file already uses for Spec 12's insertion point (its
header comment, lines ~45-64), the reference builder calls the same renderer at
the same position; every fixture there publishes no snapshot, so it returns `""`
and the assertion is unchanged. Belt and braces: `_composition_from_segments`
skips empty-text segments anyway (`runner.py:182-184`), so a mis-implemented
"emit empty" variant would still not change the joined bytes — but the contract
is omission.

**Acceptance.** With no snapshot published, `_pm_prompt(store)` is byte-identical
to today and the composition block carries no `governance_state` category. With
one published, the joined prompt contains the block exactly once, at the
specified position, and the composition reports its tokens under
`governance_state`.

## Item 6 — One renderer, reused by SPEC-23's last-word turn

**Design.** [SPEC-23](SPEC-23-continue-by-default.md) Item 2 specifies a
`_last_word_prompt` carrying "the detector and its threshold" plus "the evidence
it computed". That evidence **is** this renderer's output, focused:

```python
detector_state.publish(ledger, c, policy, focus=stop.stop_reason)   # force the tripped reading in
evidence = detector_state.prompt_text(ledger, focus=stop.stop_reason)
```

`focus` does two things: it renders the focused detector's line **unconditionally**
(it has tripped, so proximity is moot) and **first**; and it swaps the header for
the last-word framing (*"…the following reading has reached its window"*) while
keeping rules 1–3 of Item 4 intact. Every other near reading still follows,
because a PM asked to propose an alternative should see the rest of the board —
that is precisely the "same model, radically less information" defect.

`publish` is called with the live counters at the trip, so the last-word turn
never reads a snapshot from the previous quiescent point.

**Δ note — why this direction of dependency.** SPEC-23 ships first (it is the
Phase 1 keystone and is deliberately useful without this spec), so at its landing
`_last_word_prompt` formats its own evidence string. This spec **replaces** that
body with the two calls above and deletes the ad-hoc formatting. The alternative
— a second evidence renderer living beside this one — is the exact duplication
this batch keeps paying for, and would guarantee that the numbers in the standing
prompt and the numbers in the intervention prompt eventually disagree. A test
asserts `_last_word_prompt` contains no threshold formatting of its own (Testing).

**Acceptance.** `_last_word_prompt`'s source contains a `detector_state.` call and
no independent threshold arithmetic or window-phrasing literal. The evidence a
last-word turn carries for `gate_not_improving` is byte-identical to the focused
render of the same snapshot.

---

## Implementation notes

- **New: `python/errorta_council/coding/detector_state.py`** — `publish`,
  `snapshot`, `prompt_text`, plus the pure `trigger()` proximity helper. Imports
  stdlib only, with function-local `.attention` (the `_maybe_raise_monitor`
  idiom, `autonomy.py:925`); never imports `autonomy` or `runner`, per the
  `gate_state.py:21-25` discipline. Fully guarded: every function degrades to its
  empty answer rather than raising, because two of the three are on prompt-assembly
  and loop paths.
- **`python/errorta_council/coding/autonomy.py`** — `governance_proximity` on
  `CodingAutonomyPolicy` (`:65`), `policy_to_dict` (`:253-296`),
  `policy_from_dict` (`:299`); the `publish` call in `_run_sequential_loop` after
  `:1921-1923` and in `_run_concurrent_loop` after `:2306-2308`; a `publish(…,
  focus=None)` clear at loop entry; `_convergence_window_stats` extracted from
  `_account_convergence_clamp` (`:1177-1187`). **No detector's logic, threshold,
  or return value changes.**
- **`python/errorta_council/coding/runner.py`** — the one segment in
  `_pm_prompt_segments` (`:2376-2396`, position per Item 5); clear
  `detector_state` in the run-start `set_run_state` (`:6299-6300`) so a resumed
  run never renders the previous run's readings; the SPEC-23 `_last_word_prompt`
  body (Item 6). `_pm_prompt` (`:2267`) and `_pm_prompt_segments`' signatures are
  unchanged — the renderer takes the `store` the builder already has.
- **`ledger.py`** — untouched. `detector_state` is one more key in the existing
  `run_state` doc (`:1469-1498`); no migration, `get_run_state` merges over its
  defaults so an old `run_state.json` reads as absent.
- **`errorta_app/routes/coding.py`** — untouched. The run payload's `counters`
  (`:2536-2544`, read at `:2269-2272`) is the terminal post-mortem and stays as
  it is.
- **`docs/coding/PM_REFERENCE.md`** — the F145 canary asserts its embedded JSON
  equals `policy_to_dict(CodingAutonomyPolicy())`
  (`python/tests/coding/test_f145_pm_reference.py`), so `governance_proximity`
  must be added there in the same PR or CI fails. Feature, not friction.
- **No new stop reason, no new action, no schema version, no CLI change.**

## Edge cases

- **A resumed or checkpointed run.** `run_coding_loop` does `c = counters or
  LoopCounters()` (`autonomy.py:900`) and the only production caller passes none
  (`routes/coding.py`), so every window re-arms on resume. The stale
  `detector_state` from the previous run must therefore be **cleared at run
  start** or the resumed run's first PM turn reads readings that no longer exist.
  This spec does not attempt to fix the re-arming itself — SPEC-23 records it as
  a standing finding.
- **The concurrent loop dispatching a PM turn while workers are in flight.** The
  dispatch phase (`autonomy.py:2078-2175`) runs before the quiescent detector
  block, so a PM Plan can read a snapshot published at the previous quiescent
  point. That is bounded and honest: the block states the iteration it was
  computed at, and every rendered window is iteration-indexed, so it cannot have
  advanced without an iteration the PM will see next time. The alternative —
  publishing mid-dispatch — would publish counters the apply phase is mutating.
- **A detector disabled by policy** (`gate_stall_limit=0`, `wedge_stall_limit=0`,
  `plan_streak_limit=0`, `revise_livelock_limit=0`, `convergence_window=0` — all
  the module's "0 disables" knobs): never rendered. Telling a PM about a window
  that cannot fire is pure noise.
- **A threshold of exactly 1.** `trigger = min(0, 1) = 0`, so `near` is false
  forever and the reading appears only in a `focus` render — i.e. at the
  last-word turn, which is the only moment it could still matter. Stated so a
  future operator who sets `pm_idle_limit=1` knows they have opted out of the
  warning, not hit a bug.
- **`max_model_calls=None`** (the default): the budget line reports iterations
  only. A `None` cap has no proximity.
- **An ungoverned run** (`GovernanceStore.load_state().mode == "off"`). Unlike
  `_maybe_raise_monitor`, which returns early for those runs
  (`autonomy.py:929-930`), this segment is **not** gated on governance mode. The
  early return there exists because an attention Problem needs a governance phase
  to key on; a prompt does not, and an unattended ungoverned run is exactly the
  case where the PM is the only reader there will ever be.
- **Many open attention signals.** Cap the list at 5 by `created_at` order
  (`attention.list_all` already sorts, `attention.py:184`) and report the residual
  count. The block's total is capped regardless.
- **A ledger hiccup mid-publish.** `publish` is wrapped like every other
  best-effort loop writer (`autonomy.py:1209-1212`); the stale snapshot survives
  and the block's stated iteration makes the staleness visible. `prompt_text`
  returns `""` on any exception, so a corrupt snapshot degrades to today's prompt
  rather than failing a turn.
- **A PM assist turn** (`_pm_assist_prompt`, `runner.py:2399-2416`). Not given
  the segment: it is task-scoped by construction — "split or re-scope this task"
  — and SPEC-23 Item 4 turns on keeping that distinction from the run-scoped
  question. Run-level telemetry belongs to the run-level turns (plan, last word).

## Testing

- **The absence lock (the important one).** `test_prompt_segments_golden.py`
  passes **unmodified** for a run with no published snapshot; and separately, a
  fixture whose counters are all below their triggers publishes `None` and
  produces a `_pm_prompt` byte-identical to the pre-spec string. This is the
  SPEC-12 Item 3 discipline applied verbatim.
- **The presence-and-accuracy test.** Counters set at `pm_idle=1`,
  `plan_streak=4`, gate flat 5 with a failing command: the block names exactly
  those three plus budget, with those exact numbers and the live thresholds; a
  fourth detector below its trigger appears nowhere in the string.
- **The seam round-trip**, parametrized over every row of Item 2's table: set the
  counter, run one iteration, assert `get_run_state()["detector_state"]` carries
  the value **and** that `prompt_text` renders it. This is what catches a counter
  that was renamed on `LoopCounters` and silently stopped being published.
- **The proximity table** as a data-driven test over Item 3's worked column —
  `trigger(2)==1`, `trigger(8)==5`, `trigger(3)==2`, `trigger(1)==0`,
  `trigger(200)==120` — plus `governance_proximity=0.0` reproducing today's
  bytes.
- **The both-chains lock**, in the `test_spec12_18_prep.py` grep style
  (`inspect.getsource`, e.g. `:250`): both `_run_sequential_loop` and
  `_run_concurrent_loop` bodies contain a `detector_state.publish(` call. A
  behavioural test can pass on one chain by accident of scheduling; the grep
  cannot.
- **The last-word reuse lock** (Item 6): `inspect.getsource(_last_word_prompt)`
  contains `detector_state.` and does **not** contain independent window
  phrasing; and the focused render for a tripped `gate_not_improving` equals the
  evidence the intervention carries, byte for byte.
- **The framing lock**: over a matrix of near-reading combinations, the rendered
  block contains none of a fixed blacklist (`"you should"`, `"you must"`,
  `"declare done"`, `"stop now"`, `"give up"`, `"before the run is killed"`) and
  does contain the fixed anti-done sentence. Plus the `last_word_available`
  branch: with `last_word_limit=0` the closing line is the halt wording, not the
  intervention wording.
- **The drift canary**, in the style of `test_every_engine_stop_reason_is_triaged`
  (`python/tests/cli/test_runctl_mutations.py:547`): every member of SPEC-23's
  `HEURISTIC_STOP_REASONS` either has a snapshot entry or appears in an explicit
  `_NOT_RENDERED` mapping with a stated reason. A detector added later without a
  decision fails the build — the whole point being that invisibility is how this
  gap happened the first time.
- **The write-elision test**: publishing an unchanged snapshot twice performs one
  `set_run_state` call (spy the store), so a quiet 200-iteration run does not
  rewrite `run_state.json` 200 times.
- **The no-expensive-reads test**: with every counter below its trigger,
  `_dispatch_wedge_culprits` and `_open_backlog_shape` are never called during
  `publish` (spy them). Cost control asserted, not assumed.

## Documentation

- **`docs/coding/PM_REFERENCE.md`** — `governance_proximity` in the policy table
  (required by the F145 drift lock), and a short "what the GOVERNANCE STATE block
  is" note: it is a reading, it is bounded, it is absent when nothing is near, and
  it never carries an instruction.
- **`docs/CLI.md`** — one line: the same readings are already in
  `run_state.detector_state` for a run in flight, so an operator debugging "why
  did it stop" has the pre-stop telemetry, not only the post-mortem `counters`.
- **`ROADMAP-autonomy.md`** — mark SPEC-24 as specified; it is G2's information
  half, and its success criterion is criterion #2's precondition: an intervention
  is only a *real* chance to continue if the PM could see what it is arguing
  about.

## Out of scope / follow-ups

- **Surfacing the snapshot in `errorta status` / the desktop app.** It lands in
  `run_state` for free and `status` already reads that document
  (`python/errorta_cli/commands/status.py`); rendering it — a live "what is close
  to tripping" line — is a small, separate, operator-facing change.
- **The same block for DEV / reviewer / tester prompts.** Almost certainly wrong
  (they cannot act on run-level windows), named so it is a decision.
- **Per-detector proximity bands**, and a PM-visible *history* of a reading
  (a sparkline of the gate score across iterations) rather than its current
  value. Both are refinements only worth making once we have observed the PM
  using the current value at all.
- **Letting the PM respond to a reading in-band** — "this window is wrong for my
  situation, extend it". That is G1/SPEC-25 negotiation, and it needs a typed
  channel this spec deliberately does not open.
- **Fixing the resume re-arm.** Every detector window silently resets on
  `errorta continue`; this spec makes that legible and SPEC-23 records it, but
  neither fixes it.
- **`no_actionable_work` and the `Complete` path.** It has no window to render
  because it is not a detector; SPEC-27 owns it.
