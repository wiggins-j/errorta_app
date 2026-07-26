# Roadmap — making the council actually autonomous

**Source:** three live gravity-golf runs (2026-07-24 and 2026-07-26) + the
citation-grounded failure analysis in
`docs/coding/multi-agent-pipeline-failure-report.md`.
**Status:** proposed
**Owner:** wiggins-j

---

## The evidence

Three autonomous runs. None produced a working product. But the reason changed,
and the change is the whole point:

| Run | What the team did | Why it ended |
|---|---|---|
| 2026-07-24 | 96 PRs, 30% merge rate, 14-deep revise chains | **budget** — a reviewer demanded execution evidence no role could produce |
| 2026-07-26 #1 | **6 PRs, 6 approved, 6 merged, 0 revises, 0 context requests** | `gate_not_improving` — on a gate that was **green** |
| 2026-07-26 #2 | game loop merged, 2 PRs open, 7 merged total | `no_progress` — the PM was **locked out** of a legal turn by the schema |

Run 1 was a quality failure. Runs 2 and 3 were not: **the team was working and
the harness stopped it.** In run 3 the PM diagnosed the problem correctly and
recorded it —

> *"Task t-b246852339c8 'HUD overlay for strokes and par' is a duplicate of task
> t-ea32bd57fd05 with identical title. Only t-ea32bd57fd05 should be executed."*

— then tried four times to act on it and was rejected every time, because
"prune duplicates, add nothing, not done" was inexpressible. Each attempt to
comply counted as idleness, so **trying to fix the problem accelerated its own
termination.**

## The diagnosis

Not "the model isn't smart enough." The PM is the same model that diagnosed
these bugs from the outside. The difference is **information and authority**, and
that is architecture.

Count the primitives. The engine can end a run **~12 ways** — `no_progress`,
`not_converging`, `gate_not_improving`, `planning_churn`, `dispatch_wedged`,
`revise_livelock`, `completion_blocked`, `worker_unproductive`,
`member_unhealthy`, `hard_blocker`, `delivery_review_stalled`,
`budget_exhausted`. It can **recover roughly two** ways: the F127 escalate-up
ladder and GL04's convergence clamp.

That asymmetry is the product defect. It was earned honestly — the 2026-07-24
run looped forever, so the batch that followed added stop conditions. We
over-corrected into a system that halts eagerly on runs that are working. **For
an unattended product, halting is not the safe default; it is the failure.**

Underneath sit four structural gaps:

**G1 — Enforcement without negotiation.** Every layer added constrains an agent
(Spec 15's task lint, Spec 16's breaker, GL02's lanes, Spec 20's budget). None
gives an agent a channel to say *"this constraint is wrong for my situation"* or
*"I cannot express what I need to do."* Four unsatisfiable-constraint bugs have
now shipped and been fixed one at a time — the original review gate, plus the
three in Spec 21. Nothing structurally prevents the fifth.

**G2 — The harness judges without showing evidence or hearing a defence.**
Detectors compute between turns, read the ledger, and terminate. The PM's prompt
contains no hint that a convergence detector is 6 iterations into an 8-iteration
countdown, and it is never asked before the axe falls. You cannot course-correct
against a threshold you cannot observe.

**G3 — Failures are undiagnosable.** `_launch` sends the sidecar's stdout and
stderr to `DEVNULL`, so a real sidecar's traceback is *permanently
unrecoverable*. A `500` on project creation survived three debugging sessions
and is still unexplained. Version identity lied for a full release cycle. When
the system fails, it fails silently.

**G4 — Duty without capability, still.** GL05's topology audit fires on every run
("TESTER's duty demands execution but its manifest grants no executor") and never
resolves, because it is advisory. The same capability–duty mismatch the whole
Spec 12–18 batch was written to remove persists at the role level.

And one thing we cannot yet claim at all: **no run has ever completed.** Every
fix so far has bought more iterations, not a finished artifact.

## The principle

> An autonomous run should end for exactly two reasons: **the work is done**, or
> **a human/budget said stop.** Every other condition is a signal to *change
> strategy*, not to terminate — and the component best placed to choose the new
> strategy is the PM, which today is never asked.

This is the one place the research is unambiguous: the failure report's strongest
positive finding is that an expert intervening **at decision points** solved
22.2% of otherwise-unsolvable instances — strategic oversight beats more
iteration. We built the iteration limits and none of the oversight.

## Roadmap

Four phases, seven specs. Ordered so each phase makes the next one verifiable.

### Phase 1 — See failure, survive failure

| Spec | Title | Why first |
|---|---|---|
| **SPEC-22** | Diagnosable failures | You cannot fix what leaves no trace. Sidecar stdio to a real log, error correlation ids, tracebacks on the ledger, no partial writes on a failed create. This is the cheapest spec here and it unblocks every future debugging session. |
| **SPEC-23** | Continue-by-default | The keystone. A *heuristic* stop becomes a **last-word turn**: "I am about to halt for X — propose a concrete next action or confirm." Only hard stops (budget, cancel, unrecoverable) terminate immediately. Alone, this converts both 2026-07-26 stops into continuations. |

### Phase 2 — Give the agent information and a voice

| Spec | Title | Why |
|---|---|---|
| **SPEC-24** | Governance visibility | Render live detector state into the PM prompt (gate score history, idle count, revise depth, convergence ratio, budget remaining). Closes G2's information half. |
| **SPEC-25** | Expressibility and negotiation | No turn shape may be unsatisfiable: a "blocked / no-op with reason" intent is always legal; a typed channel for *"I need capability X"* (SPEC-15 deferred this explicitly); schema rejections stop counting as idleness; corrective prompts show the accepted shape instead of a raw Pydantic dump. Closes G1. |

### Phase 3 — Structural correctness

| Spec | Title | Why |
|---|---|---|
| **SPEC-26** | Role capability closure | Make grant-or-delete binding at seat time: a role whose duty needs an executor either gets one or is not seated (explicit override allowed). Closes G4 — and retires an advisory that has fired on every run and never resolved. |
| **SPEC-27** | Convergence as control, not kill | Rework the detector family so the default action is to *narrow* — clamp fan-out, escalate, re-plan — and terminate only when interventions are exhausted. GL04's hysteretic clamp is the model; generalise it across the ~12 stop reasons. |

### Phase 4 — Prove it

| Spec | Title | Why |
|---|---|---|
| **SPEC-28** | End-to-end autonomy acceptance | A repeatable gravity-golf-shaped fixture driven to `definition_of_done`, asserting a *playable artifact*, runnable in CI. Without this, every fix above is unverified in aggregate — which is exactly the state we are in today. |

## Sequencing and dependencies

- **22 → 23** — the last-word turn needs its decisions traceable, or we are
  debugging blind again.
- **24 → 23 (soft)** — the last-word turn is far better when the PM can see the
  detector state that triggered it; ship 23 first, enrich with 24.
- **25 is independent** and can land in parallel; it is the highest-value
  standalone fix after 23.
- **27 depends on 23** — "narrow instead of kill" needs somewhere to escalate to.
- **28 last**, and it is the only item that can prove the roadmap worked.

## Explicitly not in scope

- New agent roles, or removing the multi-agent structure. The failure report
  found no accuracy edge for multi-agent, but that is a product question, not
  this roadmap's.
- Model/provider changes. Every failure here reproduced on a frontier model in
  every seat; none is a capability problem.
- Retuning thresholds. Tuning is what produced this state — the point is to
  change what a threshold *does*, not what it equals.
- Backfilling docs for Spec 20/21 (shipped code-only). Worth doing; not on the
  critical path.

## How we will know it worked

1. A run ends `definition_of_done` with a playable artifact (SPEC-28 asserts it).
2. No run ends on a heuristic stop without a recorded PM intervention that was
   given a real chance to continue.
3. Any `500` or turn rejection leaves a traceback and a correlation id.
4. The topology advisory either resolves or the role is not seated. **(SPEC-26 —
   landed, and mechanically checkable.** Two honest readings, recorded rather than
   glossed. For the TESTER the answer on a headless greenfield run is *not seated*,
   because no engine path can register a unit-scoped test command — correct until
   `gate_bootstrap` gains a unit-scoped arm. For the ungrounded REVIEWER the answer
   is *seated under protest*: unseating it wedges every run, because the merge gate
   demands reviewer approval and the engine has no reviewer-less merge path.
   **Follow-up, and the top one out of this spec:** give the engine a reviewer-less
   merge path so the 26–92% false-rejection seat can actually be taken off the
   board.)
5. The count of ways to *recover* is no longer an order of magnitude smaller than
   the count of ways to *die*.
