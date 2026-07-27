# Spec 27 — Convergence as control, not kill

**Source:** [`ROADMAP-autonomy.md`](ROADMAP-autonomy.md) Phase 3 (SPEC-27), and the
asymmetry it counts: *"The engine can end a run ~12 ways … It can recover roughly
two."*
**Target version:** v0.1 (engine — `python/errorta_council/coding/autonomy.py`)
**Depends on:** [SPEC-23](SPEC-23-continue-by-default.md) (the last-word turn is
where this spec escalates *to*)
**Status:** implemented
**Owner:** wiggins-j

---

## Problem

Nine `_account_*` detectors compute between turns from the ledger. Seven of them
share one signature — `(ledger, c, policy) -> Optional[LoopResult]` — whose entire
vocabulary is **"continue" or "die"**:

| Detector | Definition | Its only non-`None` return |
|---|---|---|
| `_account_convergence` | `autonomy.py:1048` | `LoopResult(NOT_CONVERGING, c)` `:1069` |
| `_account_gate_stall` | `:1072` | `LoopResult(GATE_NOT_IMPROVING, c)` `:1118` |
| `_account_revise_livelock` | `:1256` | `LoopResult(REVISE_LIVELOCK, c)` `:1300` |
| `_account_planning_churn` | `:1327` | `LoopResult(PLANNING_CHURN, c)` `:1353` |
| `_account_dispatch_wedge` | `:1417` | `LoopResult(DISPATCH_WEDGED, c, detail=…)` `:1455` |
| `_account_foundation_stall` | `:945` | *(none — signature is `-> None`)* |
| `_account_hot_file_freeze` | `:1013` | *(none — signature is `-> None`)* |
| `_account_convergence_clamp` | `:1150` | *(declares `Optional[LoopResult]`; **always** returns `None`, `:1200`)* |
| `_account_member_outcome` | `:1746` | an F120 payload tuple, not a `LoopResult` |

Sixteen stop-reason constants live at `autonomy.py:40-55`; twelve of them are
FAILURE-class in the CLI (`errorta_cli/runstream.py:67-72`). Against that, the
engine has **two** recovery mechanisms:

1. **The F127 escalate-up ladder** — `_handle_unproductive` (`:1513-1664`): retry
   the same member (`:1531-1532`) → model escalation to a strictly stronger route
   (`:1544-1591`) → member exclusion + reassignment (`:1608-1631`) → PM assist
   (`:1633-1650`) → terminal `WORKER_UNPRODUCTIVE` (`:1652-1657`). It is
   **task-scoped**: every rung keys on one `(member_id, task_id)` pair.
2. **GL04's convergence clamp** — `_account_convergence_clamp` (`:1150-1200`): the
   **only** detector in the file that narrows instead of killing. It reads a
   windowed superseded-ratio / merge-rate over the last `convergence_window`
   resolved PRs (`:1177-1187`), and on the trip band sets a run-state flag
   (`_engage_convergence_clamp`, `:1203-1233`) that `runtime_cap` honours by
   returning `1` (`:569-570`), records a decision, and raises one deduped alert.
   It **releases** through a separate, tighter band (`:1197-1199`,
   `_release_convergence_clamp`, `:1236-1253`) — hysteresis, so it cannot flap.
   Its docstring states the invariant that makes it safe: it *"never makes a
   dispatchable task non-dispatchable"* (`:1164-1166`), so it can narrow a run
   without becoming the wedge it is diagnosing.

GL04's clamp is the proof that the softer shape works in this codebase. It is
also the only instance of it. Every other detector, on the iteration its window
expires, converts a *diagnosis* into a *termination* with nothing in between.

**This is the product defect the roadmap names.** For an unattended product,
halting is not the safe default; it is the failure. The default action for a
convergence problem should be to **narrow** — clamp fan-out, force integration,
escalate to the PM, re-plan — and to terminate only when the interventions are
exhausted.

## Why the existing machinery didn't catch it

- **There is no third return value.** A detector that wanted to say *"clamp
  concurrency and keep going"* would have to reach around its own contract and
  mutate run state as a side effect while returning `None` — which is precisely
  what `_account_convergence_clamp` does (`:1195`, `:1199` mutate; `:1200`
  returns `None`). It works, but it is invisible to the caller: the loop cannot
  distinguish "nothing happened" from "I just narrowed the run", cannot count
  interventions, and cannot bound them.
- **The detector chains are duplicated, and drift is a live hazard.** Every
  detector is wired twice — the sequential loop (`_run_sequential_loop`, `:1778`)
  and the concurrent loop (`_run_concurrent_loop`, `:2016`). Verified call-site
  groups:

  | Detector | Definition | Sequential | Concurrent |
  |---|---|---|---|
  | `_account_planning_churn` | `:1327` | `:1893-1895` | `:2277-2279` |
  | `_account_foundation_stall` | `:945` | `:1899` | `:2283` |
  | `_account_hot_file_freeze` | `:1013` | `:1900` | `:2284` |
  | `_account_convergence` | `:1048` | `:1901-1903` | `:2285-2287` |
  | `_account_gate_stall` | `:1072` | `:1907-1909` | `:2289-2291` |
  | `_account_convergence_clamp` | `:1150` | `:1914` | `:2297` |
  | `_account_revise_livelock` | `:1256` | `:1916-1918` | `:2302-2304` |
  | `_account_dispatch_wedge` | `:1417` | `:1921-1923` | `:2306-2308` |

  Plus the non-detector stop sites, also duplicated: `delivery_review_stalled`
  (`:1837-1838` / `:2210-2212`), `completion_refused` (`:1843-1847` /
  `:2216-2219`), `member_unhealthy` (`:1853-1861` / `:2224-2231`),
  `worker_unproductive` (`:1866-1869` / `:2234-2240` and `:1873-1878` /
  `:2243-2248`), `hard_blocker` (`:1880-1882` / `:2249-2251`), `no_progress`
  (`:1885-1887` / `:2272-2274`). Spec 16 already recorded this as a lesson learned
  in the code (`:2298-2301`: *"wiring BOTH chains is the dead-code lock"*).
- **The two ladders that exist are task-scoped.** F127 escalates one task; F128
  (`_handle_completion_refused`, `:1699-1719`) re-prompts one false-done claim.
  Neither can answer *"this run is churning — narrow it"*.
- **SPEC-23 gives the run one voice, once.** It converts a heuristic stop into a
  single PM last-word turn before terminating. That is the right *last* rung. It
  is not a *first* rung: it costs a model call, and three of the five heuristic
  detector conditions (wide churning fan-out, un-integrated approved work, a
  planning-only run) have a zero-model-call remedy that should be tried first.

## Goals

- Replace the `Optional[LoopResult]` detector contract with a **uniform, richer
  outcome**: `None` | `Narrow(action)` | `Escalate(reason)` | `Stop(reason)`, and
  make the loop apply each uniformly, on **both** chains.
- Give each detector an ordered, bounded **intervention ladder** whose rungs are
  recorded, whose narrowing rungs cost **zero model calls**, and whose terminal
  rung is byte-identical to today's stop.
- Define **progress** precisely (reusing GL04's metric and Spec 16's merge
  signal) as the one condition that resets a ladder.
- Compose with — never duplicate — the F127 ladder and GL04's clamp.
- Keep every stop-reason string and every CLI exit code **unchanged**.

## Non-goals

- **Not deleting a detector.** Each encodes a real observed pathology. This spec
  changes what a threshold *does*, not what it equals.
- **Not retuning a threshold.** Not one policy default in `:67-250` changes.
  (Two new fields are added; no existing value moves.)
- **Not adding a detector, a stop reason, a role, or a schema version.**
- **Not making stops advisory.** A run that exhausts its ladder stops exactly as
  today, with today's reason and today's exit code.
- **Not re-plumbing the last-word turn.** SPEC-23 owns the PM intervention; this
  spec only decides *when* a detector reaches it.

---

## Item 1 — The detector outcome contract

**Design.** Three frozen dataclasses and a union alias, beside `LoopResult`
(`:830-834`):

```python
NARROW_CLAMP_FANOUT      = "clamp_fanout"        # GL04's existing run-state flag
NARROW_FORCE_INTEGRATION = "force_integration"   # drain in-flight, merge what's mergeable
NARROW_CLAMP_PLANNING    = "clamp_planning"      # no further Plan dispatch until a worker runs
NARROW_FORCE_LIFT        = "force_lift"          # F159's existing freeze force-lift
NARROW_ALERT_ONLY        = "alert_only"          # F139 WS-A's existing stall heartbeat

@dataclass(frozen=True)
class Narrow:
    action: str                       # one of the NARROW_* constants
    detector: str                     # the stop reason this ladder defers
    evidence: str                     # the string _maybe_raise_monitor already gets
    detail: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class Escalate:
    reason: str                       # the stop reason that WOULD have fired
    detector: str
    evidence: str
    detail: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class Stop:
    reason: str
    detector: str
    evidence: str
    detail: dict[str, Any] = field(default_factory=dict)

DetectorOutcome = Optional[Narrow | Escalate | Stop]
```

**How the loop applies each** — one helper, `_apply_detector_outcome`, called from
both chains:

| Outcome | Loop action | Model calls | Short-circuits the chain? |
|---|---|---|---|
| `None` | nothing | 0 | no (today's behaviour) |
| `Narrow(action)` | engage the action (Item 2), record a decision, raise one deduped alert per episode, advance that detector's rung, **continue the loop** | 0 | **no** |
| `Escalate(reason)` | hand to SPEC-23's `_intervene(…, LoopResult(reason, c, detail))`. `None` back → continue (SPEC-23 reset map applied). A `LoopResult` back → return it | ≤1 (SPEC-23's budget) | **yes** |
| `Stop(reason)` | `return LoopResult(reason, c, detail)` — byte-identical to today | 0 | **yes** |

**Δ note — why `Narrow` must not short-circuit and `Escalate`/`Stop` must.** Today
the chain is a sequence of early returns: `None` falls through to the next
detector, a `LoopResult` returns immediately. Mapping `Narrow → fall through` and
`Escalate`/`Stop → return` makes the new contract a **strict generalisation** of
the existing control flow, so a chain in which every detector returns `None` or
`Stop` executes exactly the instruction sequence it executes today. This is also
already the shape of the code: `_account_convergence_clamp` is a pure-`Narrow`
detector wired *before* Spec 16's stop in both chains and deliberately does not
short-circuit (`:1914`, `:2297`).

**Δ note — the three already-narrowing detectors become uniform, not new.**
`_account_foundation_stall` (`:945`) returns `Narrow(NARROW_ALERT_ONLY)`,
`_account_hot_file_freeze` (`:1013`) returns `Narrow(NARROW_FORCE_LIFT)` on the
iteration it force-lifts, and `_account_convergence_clamp` (`:1150`) returns
`Narrow(NARROW_CLAMP_FANOUT)` on the engage transition. All three keep their
current side effects verbatim; only the return value becomes legible to the
caller. That is what makes them countable, and therefore boundable (Item 4).

**Acceptance.** All nine `_account_*` functions share the `-> DetectorOutcome`
signature. A chain of `None`/`Stop` returns produces a trace byte-identical to
today's. `_account_convergence_clamp` still never produces a `Stop`.

## Item 2 — The intervention ladder

**Design.** A ladder is an **ordered tuple of rungs declared per detector**. The
machinery is uniform; the rung list is not, because narrowing a wedge makes it
worse while narrowing a churning fan-out fixes it. Four rung kinds:

| Rung | Mechanism | Cost | Release / bound |
|---|---|---|---|
| **`NARROW_CLAMP_FANOUT`** | GL04's existing engage path (`:1203-1233`): `set_run_state(convergence_clamped=True)`; `runtime_cap` returns `1` (`:569-570`) | 0 model calls, 0 extra iterations | GL04's own hysteretic release band (`:1197-1199`) — **unchanged, not duplicated** |
| **`NARROW_FORCE_INTEGRATION`** | new run-state flag `integration_only=True`: the dispatch phase stops adding `Assign` work and lets `decide_next`'s existing merge-first branch (`topology.py:263-272`) drain every `mergeable` PR, then the due gate (`topology.py:273-278`) | 0 model calls; ≤ `narrow_drain_iters` iterations | clears when no PR is `mergeable` and none is in flight, **or** at `narrow_drain_iters` iterations (force-lift, Item 6) |
| **`NARROW_CLAMP_PLANNING`** | new run-state flag `planning_clamped=True`: refuse a `Plan`/`PMAssist` dispatch while any worker head is dispatchable, so the next turn is a worker turn | 0 model calls; ≤ `narrow_drain_iters` iterations | clears on the first worker turn (which already resets `c.plan_streak`, `_apply_outcome:2343-2346`) or at `narrow_drain_iters` |
| **`ESCALATE`** | SPEC-23's last-word PM turn, unchanged | ≤1 model call, drawn from SPEC-23's run-wide `last_word_limit` | SPEC-23's own bounds (Item 3 there) |
| **`STOP`** | `LoopResult(reason, c, detail)` | 0 | terminal |

**Rung ordering principle.** Cheap, mechanical, zero-model-call narrowing runs
first and often; the judgement-heavy, run-scoped escalation runs at most twice per
run; the stop is last. This is exactly the ordering F127 already uses
(`:1531-1657`: retry → stronger route → different member → PM → terminal), and
exactly the ordering GL04 already asserts between its clamp and Spec 16's stop
(GL04 §"Escalation ladder": *"the clamp buys the run a chance to drain before the
stop fires"*).

**Progress — the one condition that resets a ladder.** Defined precisely, reusing
what already exists; no new notion of progress is introduced:

> **PROGRESS** since a ladder entered its current rung at merged-count `M₀` is
> either
> **(a)** `merged_pr_count > M₀` — the exact signal `_account_revise_livelock`
> already trusts to reset its own window (`:1286-1292`, *"any merge anywhere is
> progress"*), or
> **(b)** GL04's window has recovered past the **release** band —
> `superseded_ratio ≤ convergence_release_ratio` **and**
> `merge_rate ≥ convergence_release_merge_rate` (`:1197-1199`), computed over the
> last `convergence_window` resolved PRs (`:1177-1187`).

Clause (b) is the sharper signal once the run has resolved a full window; clause
(a) covers the pre-window case, where GL04's metric deliberately abstains
(`:1180-1181`, *"a metric off 2 resolved PRs is noise"*).

On PROGRESS, every ladder's **rung index** resets to 0 and its `M₀` is
re-anchored. **Narrowing flags are not cleared by a ladder reset** — each keeps
its own release condition (GL04's hysteresis; the drain/planning clears above).
Two release paths for one flag is how a clamp starts flapping, and hysteresis is
the entire reason GL04's bands are separate.

**Acceptance.** A run that clamps, then merges a PR, has its rung index back at 0
while `convergence_clamped` still reflects GL04's band. A ladder that reaches its
last rung stops with today's reason. Rung transitions are ledger decisions.

## Item 3 — Per-detector rung mapping

**Design.** The table *is* the spec; the code holds it as one module-level dict
beside the stop-reason constants (`:40-55`), and a test asserts it covers exactly
the HEURISTIC set SPEC-23 Item 1 defines.

| Detector / stop reason | Trip site | Rungs, in order | Why |
|---|---|---|---|
| `not_converging` (`_account_convergence`, `:1048`) | `:1069` | `FORCE_INTEGRATION` → `CLAMP_FANOUT` → `ESCALATE` → `STOP` | quiescence with approved-but-unmerged work is the cheapest recoverable shape; integration is tried before spending a turn. The clamp follows so a re-armed run cannot immediately re-fan-out into the same stasis |
| `gate_not_improving` (`_account_gate_stall`, `:1072`) | `:1118` | `ESCALATE` → `STOP` | fan-out is not the problem — a red gate is. Escalate **carrying the failing output**: `_gate_has_failure` (`:1121-1144`) and `_gate_fingerprint` (`:622-671`) already hold the per-command exit codes and the delivery verdict; today they are summarised into a monitor string (`:1114-1117`) and discarded |
| `planning_churn` (`_account_planning_churn`, `:1327`) | `:1353` | `CLAMP_PLANNING` → `ESCALATE` → `STOP` | the detector's own diagnosis is "PM plan turns with zero interleaved worker turns" (`:1329-1331`). Forcing the next turn to be a worker turn *is* the remedy, and it costs nothing |
| `dispatch_wedged` (`_account_dispatch_wedge`, `:1417`) | `:1455` | `ESCALATE` → `STOP` | **no narrowing rung by construction** — narrowing a graph with nothing dispatchable makes it strictly worse. Escalate with `_dispatch_wedge_culprits` (`:1356-1414`), which already names the blocking dep ids and how many todo tasks each transitively blocks, and ask the PM to re-plan around them |
| `revise_livelock` (`_account_revise_livelock`, `:1256`) | `:1300` | *(GL04's clamp, already wired ahead of it)* → `ESCALATE` → `STOP` | **keep Spec 16 / GL04 exactly as built.** The clamp is already the rung below this stop in both chains (`:1914` → `:1916`, `:2297` → `:2302`); this spec adds only the escalate rung and does not re-implement the clamp |
| `delivery_review_stalled` (`:1837-1838` / `:2210-2212`) | staged | `FORCE_INTEGRATION` → `ESCALATE` → `STOP` | a delivery review judges the *integrated* head; draining pending merges changes the thing under review before asking anyone |
| `no_progress` (`:1885-1887` / `:2272-2274`) | inline | `ESCALATE` → `STOP` | SPEC-23's rung, unchanged. No mechanical narrowing applies to an idle PM |
| `worker_unproductive` (`_handle_unproductive`, `:1657`) | `:1866-1869` / `:2234-2240`, `:1873-1878` / `:2243-2248` | *(F127 rungs 1–4)* → `ESCALATE` → `STOP` | the ladder already exists and is task-scoped; SPEC-23 Item 4 already appends the escalate rung. This spec adds nothing — it records the composition so a reader sees F127 as an instance of the general pattern, not an exception to it |
| `completion_blocked` (`_handle_completion_refused`, `:1699`) | `:1843-1847` / `:2216-2219` | *(F128's own re-prompt loop)* → `STOP` | **no rung added.** F128 already re-prompts the PM with the open item set `completion_refused_limit` times; a second intervention asks the same party the same question (SPEC-23 Item 4) |
| `no_actionable_work` (`decide_next` → `Complete`, `topology.py:337`; returned at `:1811-1812`, `:2183`, `:2186`) | inline | `ESCALATE` **only when open work exists** → `STOP` | SPEC-23 Item 1 deferred this reason here explicitly. It is CLI **success**-class (`runstream.py:75-77`), so it escalates only when the ledger still holds open tasks or open PRs — i.e. it is a wedge wearing a success label. Otherwise it is left exactly as today. A refused escalation stops `no_actionable_work`, still `EXIT_OK` (Item 5) |
| `budget_exhausted`, `cancelled`, `checkpoint`, `hard_blocker`, `member_unhealthy`, `definition_of_done` | various | **none** | SPEC-23 Item 1 classes these HARD/DONE; a ladder here would argue with the operator, the budget, or a member's own declaration |
| `foundation_not_converging` (`_account_foundation_stall`, `:945`) | `:1007` | `ALERT_ONLY`, forever | already a pure narrow — the run continues clamped by design (`:947-950`). Listed so the contract is visibly total |
| `hot_file_freeze_stalled` (`_account_hot_file_freeze`, `:1013`) | `:1042` | `FORCE_LIFT` | already a pure narrow, and the **precedent for every force-lift in this spec** (`:1025-1027`): a narrowing that never releases is a wedge, so it is capped and lifted with a decision + monitor |

**Acceptance.** Every reason in SPEC-23's `HEURISTIC_STOP_REASONS` has a rung
tuple; every reason in `HARD_STOP_REASONS` has none; a reason added without an
entry fails the test. The last element of every non-empty tuple is `STOP`.

## Item 4 — Boundedness

This batch exists because a run looped for 3h20m on 2026-07-24. Adding recovery
rungs must not reintroduce that. Five independent bounds:

1. **Rung indices are monotone.** A ladder only ever advances, except on PROGRESS
   (Item 2), which requires a **merged PR** or a recovered GL04 window. Both
   require the run to actually integrate work.
2. **Every narrowing rung is time-capped.** `narrow_drain_iters` (policy, default
   **5**) bounds a `FORCE_INTEGRATION` or `CLAMP_PLANNING` episode; at the cap it
   **force-lifts** with a decision and a monitor signal — the exact mechanism
   `_account_hot_file_freeze` already uses (`:1025-1043`). `CLAMP_FANOUT` needs no
   new cap: it never blocks dispatch (`runtime_cap` returns `1`, never `0`,
   `:570`).
3. **A run-wide narrowing budget.** `narrow_limit` (policy, default **6**) caps
   the total number of narrowing rungs *the whole run* may engage, across all
   detectors and all ladder resets. On exhaustion every rung tuple collapses to
   its `ESCALATE`/`STOP` tail. `narrow_limit == 0` disables the entire ladder and
   restores today's behaviour exactly (the module's `max(0, …)` convention —
   `gate_stall_limit`, `plan_streak_limit`, `wedge_stall_limit`,
   `revise_livelock_limit`, `convergence_window` all use it, `:333-346`,
   `:364-369`, `:384-387`).
4. **Escalation is SPEC-23's budget, not a second one.** The `ESCALATE` rung calls
   `_intervene`; it does not get its own allowance. SPEC-23 Item 3 caps
   interventions at `last_word_limit` (default 2) run-wide, refuses a second
   intervention for the same detector without an intervening merge, and excludes
   the last-word turn from all detector accounting.
5. **The iteration and model-call budgets are untouched and still dominate.**
   Narrowing rungs consume iterations (counted at `:1824` / `:2196`) and zero
   model calls. `budget_exhausted` is HARD in SPEC-23's taxonomy and is checked
   *before* any detector runs (`:1815-1818` sequential, `:2075` / `:2184-2185`
   concurrent), so no ladder can defer it.

**The worst case, stated explicitly.** Let *N* = `narrow_limit` (6), *D* =
`narrow_drain_iters` (5), *L* = `last_word_limit` (2, SPEC-23).

- Extra **model calls** attributable to this spec: **0.** Narrowing rungs dispatch
  nothing; the only turn-spending rung is SPEC-23's, already budgeted.
- Extra **iterations**: bounded above by *N × D* = **30**, because only
  `FORCE_INTEGRATION` and `CLAMP_PLANNING` extend a run at all, each episode is
  capped at *D* iterations by bound 2, and bound 3 caps the total number of
  episodes at *N* regardless of how many detectors trip or how often ladders
  reset. `CLAMP_FANOUT` adds **0** iterations — it changes concurrency, not
  liveness.
- Against `max_iterations = 200` (`:67`) that is a ≤15% ceiling on run length,
  and it is a *ceiling*, not a cost: 30 iterations of forced integration are 30
  iterations spent merging approved work.
- **The forever-loop is unreachable by construction.** A ladder reset requires a
  merge or a recovered churn window; a merge consumes at least one iteration; the
  iteration counter is monotone and capped. So even the adversarial case —
  merge-one-PR / re-trip / reset, repeated — terminates at `budget_exhausted`,
  the same wall that bounds the run today, and reaches it no later than
  30 iterations after it would have.

**Acceptance.** `narrow_limit=0` reproduces today's trace byte-for-byte. A
synthetic run that trips every detector repeatedly engages at most `narrow_limit`
narrowing rungs and terminates within `max_iterations`. No test in the suite
observes a model-call count higher than its pre-spec value for a `narrow_limit>0`
run that never escalates.

## Item 5 — Backward compatibility

**Verified, not assumed:**

- **Stop-reason strings.** No constant in `autonomy.py:40-55` changes value, and
  no constant is added. This spec introduces no stop reason.
- **`FAILURE_STOP_REASONS`** (`runstream.py:67-72`) — 12 reasons — and
  **`SUCCESS_STOP_REASONS`** (`:75-77`) — 4 reasons — need **no edit**.
- **`classify_exit`** (`runstream.py:133-149`) is a **fail-closed allowlist**:
  `failed`/`interrupted` status → `EXIT_RUN_FAILED`; otherwise **only** a
  `stop_reason` in `SUCCESS_STOP_REASONS` → `EXIT_OK`; everything else, including
  an unknown reason, → `EXIT_RUN_FAILED`. Confirmed by reading `:143-149`. Since
  no reason is added or renamed, every exit code is unchanged by construction.
- **`STOP_REASON_GLOSS`** (`runstream.py:80-105`) and **`_TERMINAL_BAD`**
  (`errorta_cli/render/status.py:27-33`) need no edit.
- **The drift lock stays green.** `test_every_engine_stop_reason_is_triaged`
  (`python/tests/cli/test_runctl_mutations.py:547-563`) asserts the two CLI sets
  exactly partition the engine's parsed constants, with no untriaged and no
  phantom members. Adding no constant means it passes unmodified.
- **A run that exhausts its interventions stops exactly as today** — same reason,
  same `LoopResult.detail` keys, same exit code. The only visible difference is
  extra ledger decisions recording the rungs that were tried, and (from SPEC-23)
  the summary line recording the last word.

**Δ note — why `no_actionable_work` cannot flip.** It is SUCCESS-class. Its
`ESCALATE` rung (Item 3) may only *continue* the run; a refused or abstaining
escalation returns `LoopResult(NO_ACTIONABLE_WORK, …)` unchanged, which
`classify_exit` still maps to `EXIT_OK`. There is no code path in this spec that
converts a success-class reason into a failure-class one, and a test asserts it.

## Item 6 — Where it hooks, and the non-wedge lock

**Design.** Both chains, or it is dead code — the lesson already in the file at
`:2298-2301`. Each of the sixteen call sites in the Item-"Why" table routes
through one helper:

```python
def _apply_detector_outcome(ledger, members, policy, c, out, *,
                            run_turn, should_cancel) -> Optional[LoopResult]:
    """None -> keep going (nothing fired, or a Narrow was engaged).
    A LoopResult -> return it from the loop."""
```

**The two chains have different shapes and both must be respected:**

- **Sequential** (`:1778`) is quiescent by construction. Each site becomes
  `res = _apply_detector_outcome(…); if res is not None: return res`.
- **Concurrent** (`:2016`) *stages* the apply-phase stops into `pending_stop`
  (`:2210-2251`) while other futures may still be running, and returns them at a
  drain point (`:2253-2271`) or at the nothing-running early exit
  (`:2177-2186`). Therefore: **an outcome staged during the apply phase is only
  *applied* at the drain point**, never inline — a `Narrow` that mutated dispatch
  state mid-batch would change the rules under live futures. `pending_stop`
  becomes `pending_outcome`. **Both** return points must route through the helper,
  or a staged `delivery_review_stalled` escapes un-narrowed on the wedged path.
  The inline detector chain (`:2277-2308`) is already inside `if not in_flight`
  (`:2255`) and hooks like the sequential one.

**The non-wedge lock — generalised from GL04.** GL04's clamp carries the
invariant that made it safe (`:1164-1166`; GL04 §GAP-5 *"the clamp must not become
the stall it's diagnosing … it may only narrow concurrency and block new fan-out,
never make an otherwise-dispatchable task non-dispatchable, or it trips
`_account_dispatch_wedge`"*). Every `NARROW_*` action inherits it as a stated
invariant with a per-action test:

1. **It may only reduce concurrency or defer *new* work.** A run under any
   narrowing that has one dispatchable task still dispatches it, serially.
2. **It must carry a release condition** — GL04's hysteretic band, or "nothing
   left to merge", or "a worker turn ran".
3. **It must force-lift at a cap** even if its release condition never arrives,
   with a decision and a monitor signal — `_account_hot_file_freeze`'s existing
   pattern (`:1025-1043`).

`NARROW_FORCE_INTEGRATION` is the one that could violate (1) if written
carelessly: if it refused `Assign` dispatch while nothing was mergeable, the run
would go quiescent and manufacture a `dispatch_wedged` or `no_actionable_work`
out of a churn alarm. So it **engages only when at least one PR is `mergeable`
(`runner.py:4332-4360`, `_set_mergeable_if_ready`) or a merge is in flight**, and
disengages the moment that stops being true.

**Acceptance.** A grep test asserts both loop function bodies contain
`_apply_detector_outcome(` (the `test_spec12_18_prep.py` static-grep style — a
behavioural test can pass on one chain by accident of scheduling; the grep
cannot). A per-`NARROW_*` fixture with exactly one dispatchable task still
dispatches it. A `FORCE_INTEGRATION` requested with nothing mergeable is a
recorded no-op that advances the rung without consuming `narrow_limit`.

---

## Implementation notes

- **`python/errorta_council/coding/autonomy.py`** carries nearly all of it:
  `Narrow`/`Escalate`/`Stop` + the `NARROW_*` constants beside `LoopResult`
  (`:830-834`); the per-detector rung map beside the stop reasons (`:40-55`);
  `narrow_limit` / `narrow_drain_iters` on `CodingAutonomyPolicy` (`:65-250`) plus
  `policy_to_dict` (`:253-296`) and `policy_from_dict` (`:299-403`) with the
  `max(0, …)` clamp; ladder state on `LoopCounters` (`:748-827`);
  `_apply_detector_outcome` + the rung engage/release helpers next to
  `_engage_convergence_clamp` (`:1203`) / `_release_convergence_clamp` (`:1236`);
  the return-type change on all nine `_account_*` functions; the hook at all
  sixteen call sites.
- **`runtime_cap`** (`:539-578`) is **not changed**. It already honours
  `convergence_clamped` above the foundation gate (`:569-570`). The new
  `integration_only` / `planning_clamped` flags are read in the dispatch phase
  (`:2102-2175`) and by the sequential loop's `decide_next` call (`:1810`), not in
  `runtime_cap` — they gate *what* is dispatched, not *how many*.
- **`python/errorta_council/coding/topology.py`** — `plan_next_batch` (`:340`) and
  `decide_next` (`:238`) grow one optional keyword each for the two new flags,
  matching how `hot_paths` / `frozen` / `owned_paths` are already threaded
  (`:2102-2107`). `decide_next`'s merge-first branch (`:263-272`) is what
  `FORCE_INTEGRATION` leans on and is unchanged.
- **Resume/continue** — see Edge cases; this touches `runner.py:6369-6375` and
  `errorta_app/routes/coding.py:2536-2544`.
- **`docs/coding/PM_REFERENCE.md`** — the F145 anti-drift test
  (`python/tests/coding/test_f145_pm_reference.py`) asserts its embedded JSON
  equals `policy_to_dict(CodingAutonomyPolicy())`, so the two new fields must be
  documented in the same PR or CI fails. Feature, not friction.
- **No CLI change at all.** `runstream.py` and `render/status.py` are untouched by
  this spec (SPEC-23 owns the one extra summary line).

## Edge cases

- **A detector fires while work is in flight.** Cannot happen for the inline
  chain: both concurrent call-site groups sit inside `if not in_flight`
  (`:2255`), and the sequential loop is quiescent by construction. It *can*
  happen for the staged stops (`:2210-2251`) — resolved by Item 6's rule that
  staged outcomes are applied only at the drain point. A `FORCE_INTEGRATION`
  engaged at a drain point has, by definition, nothing to race.
- **Two detectors fire in one iteration — precedence.** Chain order **is**
  precedence order, unchanged from today: `planning_churn` → `foundation` →
  `hot_file` → `not_converging` → `gate_not_improving` → `convergence_clamp` →
  `revise_livelock` → `dispatch_wedged` (`:1893-1923` / `:2277-2308`). `Narrow`s
  are applied cumulatively as encountered and do not short-circuit; the **first**
  `Escalate` or `Stop` in chain order wins and the rest of the chain is not
  evaluated — exactly today's early-return semantics. A second detector requesting
  a narrowing already in effect is recorded as **satisfied**: its rung advances
  and `narrow_limit` is **not** charged twice.
- **A narrow that itself wedges.** Item 6's three-part invariant, with GL04's
  non-wedge lock (`:1164-1166`) as the precedent and
  `_account_hot_file_freeze`'s force-lift (`:1025-1043`) as the escape hatch.
  Asserted per action, not assumed.
- **Resume / continue re-arms the ladder.** Real, and it must be fixed here or
  Item 4's bound 3 is fiction. `run_coding_loop` does `c = counters or
  LoopCounters()` (`:900`) and the only production caller —
  `routes/coding.py:2534` — passes **no** counters, so **every start and every
  resume gets a fresh counter set**. The narrowing *flags* live in run state
  (`convergence_clamped`) and therefore already survive a resume, while the
  counter that bounds them would not: a checkpoint/resume cycle would hand a
  clamped run a fresh `narrow_limit`. Fix: persist the ladder state
  (`{detector: rung}` plus `narrows_used`) in the run-state counters at both
  terminal writers (`runner.py:6369-6375`, `routes/coding.py:2536-2544`) and
  rehydrate it at run start, exactly as SPEC-23 persists `last_words`. The other
  windows (`plan_streak`, `wedge_streak`, gate/progress iters) are genuinely
  per-process and re-arming them on resume stays correct.
- **A ladder rung whose mechanism is disabled by policy.** `convergence_window ==
  0` disables GL04's clamp (`:1171-1172`); a `CLAMP_FANOUT` rung then engages
  nothing. It is recorded as a no-op, advances the rung, and does **not** charge
  `narrow_limit` — the same rule as an already-satisfied narrow.
- **A run with no PM seated.** `ESCALATE` degrades to the next rung (`STOP`);
  SPEC-23's `_intervene` already returns the stop unchanged when no PM member
  exists, and `decide_next` already returns `Complete(reason="worker_unproductive")`
  in the PM-less assist case (`topology.py:298`).
- **Cancel or budget during a narrowing episode.** Both are HARD and both are
  checked before the detector chain (`:1807`/`:1815-1818` sequential,
  `:2073`/`:2075` concurrent). A narrowing flag left set at a cancel is harmless:
  it is run state, and the next start re-evaluates its release band.
- **Corrective-task pruning** (`_CORRECTIVE_PREFIXES`, `runner.py:495`, used at
  `:1370` / `:1436`) — a pruned branch must not leave a phantom rung. Clear the
  affected ladder entries at the same prune point Spec 16 / GL04 clear theirs.
- **Line-number drift in the cited specs.** Spec 16 and GL04 cite
  `_supersede_ancestors` at `runner.py:677-712`; it is now at **`:1401`**. Every
  anchor in *this* spec was re-read against the working tree at the commit named
  in the front matter. Anchors are a navigation aid, not a contract; the symbol
  names are the contract.

## Testing

- **The contract** — all nine `_account_*` functions return `DetectorOutcome`; a
  chain of `None`/`Stop` produces a `LoopResult` byte-identical to the pre-spec
  one (parametrized over every heuristic reason).
- **The generalisation lock** — `narrow_limit=0` reproduces today's trace exactly:
  same stop reason, same iteration count, same model-call count, same decision
  log. This is the "no behaviour change when off" lock and it must be the first
  test written.
- **Per-rung behaviour** — a churning fixture (GL04's 53/96-superseded shape)
  clamps rather than stops; a fixture with a `mergeable` PR and a stalled delivery
  review drains it before escalating; a `plan_streak`-saturated fixture dispatches
  a worker turn next rather than stopping.
- **The non-wedge lock, per `NARROW_*` action** — a fixture with exactly one
  dispatchable task still dispatches it under each narrowing; a
  `FORCE_INTEGRATION` with nothing mergeable is a no-op that does not charge
  `narrow_limit` and does not trip `_account_dispatch_wedge`.
- **Boundedness** — a run that trips every detector every iteration engages at
  most `narrow_limit` narrowing rungs, spends **zero** extra model calls, and
  terminates within `max_iterations`. The adversarial merge-one/re-trip/reset run
  terminates at `budget_exhausted`. Assert the **counts**, not just the outcome.
- **The force-lift lock** — a `FORCE_INTEGRATION` whose merge never lands lifts at
  `narrow_drain_iters` with a decision and a monitor signal, and the run resumes
  normal dispatch (mirrors `test_f159`-style hot-file freeze tests).
- **Precedence** — a fixture where two detectors trip in one iteration: both
  `Narrow`s applied, the first-in-chain-order `Escalate`/`Stop` taken, the later
  detector's rung untouched. And the double-charge lock: two detectors requesting
  the same narrowing charge `narrow_limit` once.
- **Both chains** — the same scenario through `_run_sequential_loop`
  (`max_parallel_workers=1`) and `_run_concurrent_loop` (`>1`) produces the same
  ladder trace, **plus** the static grep that both function bodies contain
  `_apply_detector_outcome(`.
- **Backward compatibility** — `test_every_engine_stop_reason_is_triaged`
  (`tests/cli/test_runctl_mutations.py:547`) passes unmodified; a parametrized
  assertion that every reason's `classify_exit` is unchanged pre/post spec;
  `no_actionable_work` after a refused escalation still exits `EXIT_OK`.
- **Resume** — a run checkpointed mid-ladder and resumed does **not** get a fresh
  `narrow_limit` (this is the bound-3 lock and it fails today).
- Full coding suite — `test_dispatch_wedge.py`, `test_gate_stall.py`,
  `test_planning_churn.py`, `test_spec16_revise_breaker.py`,
  `test_gl04_revise_convergence.py` — plus `ruff`.

## Documentation

- `docs/coding/PM_REFERENCE.md` — `narrow_limit` / `narrow_drain_iters` in the
  policy table (required by the F145 drift lock), and a short note on what a
  narrowed run looks like from the PM's seat: fan-out clamped to serial,
  integration forced, a re-plan requested — none of which is a punishment.
- `docs/CLI.md` — a stopped run may now carry decisions naming the interventions
  that were tried before it stopped; the stop reason and exit code are unchanged.
- `ROADMAP-autonomy.md` — mark SPEC-27 as specified. Success criterion #5 ("the
  count of ways to *recover* is no longer an order of magnitude smaller than the
  count of ways to *die*") is what this spec is measured against: it takes the
  recovery count from 2 to 2 + one bounded ladder per heuristic reason.

## Out of scope / follow-ups

- **SPEC-24 (governance visibility).** Rendering live ladder state — "you are on
  rung 2 of 4 for `not_converging`" — into the PM's standing prompt is the
  information half of G2. This spec is deliberately useful without it, and its
  escalate rung carries the evidence for its own detector regardless.
- **SPEC-25 (expressibility).** Until a typed "blocked / no-op with reason" intent
  exists, a PM whose correct answer to an escalation is *"do nothing, the drain is
  working"* cannot say so and its turn reads as an abstention. Named, not missed.
- **A learned or adaptive rung order.** GL04 §9 is explicit that online deadlock
  detection from trajectory features is an open problem and that fixed caps are
  supported only by extension from the success-envelope data. The rung tuples here
  are fixed and honest, not optimal.
- **Narrowing the *review* lane** (e.g. capping reviewer vetoes further under a
  clamp) — GL02 owns per-head veto limits and the interaction deserves its own
  measurement.
- **A human rung.** Every ladder here ends at the PM then stops, because the run
  is unattended by definition. Paging a human as a rung between escalate and stop
  is a product question.
- **Persisting the full `LoopCounters` across resume.** This spec persists the
  ladder state because its bound depends on it, and SPEC-23 persists `last_words`
  for the same reason. That every other detector window silently re-arms on
  `errorta continue` remains a real, un-owned finding.
