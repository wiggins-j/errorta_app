# Spec 22–28 — autonomy batch: sequencing, ownership, and parallel-build contract

**Specs:** [22](SPEC-22-diagnosable-failures.md) · [23](SPEC-23-continue-by-default.md) ·
[24](SPEC-24-governance-visibility.md) · [25](SPEC-25-expressibility-and-negotiation.md) ·
[26](SPEC-26-role-capability-closure.md) · [27](SPEC-27-convergence-as-control.md) ·
[28](SPEC-28-autonomy-acceptance.md)
**Roadmap:** [ROADMAP-autonomy.md](ROADMAP-autonomy.md)
**Status:** proposed · **Owner:** wiggins-j

> Successor to [SPEC-12-18-gravity-golf-batch-plan.md](SPEC-12-18-gravity-golf-batch-plan.md),
> and written against its lesson: that batch was de-conflicted well (the prep PR
> worked — four engineers, no seam collisions) but its *aggregate* behaviour was
> never tested, and the result was three runs that each failed differently. This
> batch ends with SPEC-28 for exactly that reason.

---

## The one-sentence diagnosis

The engine can end a run **~12 ways** and recover **~2**, so a healthy run gets
halted by the machinery that was supposed to protect it — and the agent best
placed to choose a different strategy is never asked.

## The one constraint that governs every choice below

**Do not reintroduce the 2026-07-24 forever-loop.** Every mechanism in this batch
that lets a run *continue* must be bounded, and the bound must be stated as a
worst-case iteration/model-call cost in its spec. When in doubt, the batch
prefers a run that stops with a legible reason over a run that spins.

---

## Phase 0 — the shared prep PR (do this first, one owner, no behaviour change)

Same move that made the 12–18 batch land cleanly: everything multiple specs touch
goes in **one** small PR with no consumers, so no two branches race the same
lines. Nothing here changes runtime behaviour **except P0.5**, which fixes two
live bugs.

### P0.1 — Land every new policy field, no consumers

| Field | Default | Spec |
|---|---|---|
| `last_word_limit` | 2 | 23 |
| `governance_proximity` | 0.6 | 24 |
| `narrow_limit` | 3 | 27 |
| `blocked_turn_limit` | 3 | 25 |
| `capability_overrides` | `{}` | 26 |

All round-tripped through `policy_to_dict` / `policy_from_dict` with the
`max(0, …)` disable convention. **`0` must restore today's behaviour exactly for
every one of them** — that is the batch's escape hatch and its regression story.

⚠️ Adding a policy field breaks `test_f145_pm_reference.py`, which asserts
`PM_REFERENCE.md`'s embedded `autonomy_defaults` JSON equals
`policy_to_dict(CodingAutonomyPolicy())`. Update the doc in the same commit —
three separate GL branches hit this and each rediscovered it.

### P0.2 — Declare the `run_state` keys

`detector_state` (24), `last_words` (23), `narrow_ladder` (27),
`role_closure` (26). Additive, absent→falsy, no migration — the established
pattern (`gate_due`, `convergence_clamped`, `foundation_status`).

**Persist at both terminal writers** (`runner.py` end-of-run and
`routes/coding.py`'s run finaliser). SPEC-23 and SPEC-27 both found the same
latent bug: `run_coding_loop` does `c = counters or LoopCounters()` and the only
production caller passes none, so **every detector window silently re-arms on
`errorta continue`** — while the *flags* those windows bound (e.g.
`convergence_clamped`) already survive in run state. A resumed run would get a
clamped state with a fresh budget. Fix the asymmetry here, once, for all of them.

### P0.3 — Extract the shared readers

- `_convergence_window_stats` out of `_account_convergence_clamp`'s inlined
  arithmetic — SPEC-24 renders it, SPEC-27 ladders on it. One computation.
- **Detector evidence becomes a value.** `_maybe_raise_monitor` already receives
  an evidence string and discards it; SPEC-23 needs it for the intervention
  prompt and SPEC-24 for the prompt segment. Make it a returned/recorded value
  rather than three independent re-derivations — the SPEC-19 "four declarations,
  two values" failure shape, avoided in advance.

### P0.4 — De-conflict the prompt seams

Three specs add or edit PM/DEV prompt content (24 `governance_state`, 25
`_corrective_turn_prompt`, 23 `_last_word_prompt`). Fix the segment order now and
apply the P0.5 golden pattern from the last batch: the reference builder in
`test_prompt_segments_golden.py` **calls** the live renderer at a fixed insertion
point, so the byte-lock survives and only the intended segment moves.

Order: `gate_output` → `governance_state` → `tool_guidance` → standing rules.

### P0.5 — Fix the two live bugs (behaviour change, deliberate)

Both are in shipped `alpha.13`, both were found while writing these specs, and
both will bite the very next run:

1. **Spec 21's fix is half-done.** `runner.py` still computes
   `made_progress = len(created) > 0`, so the decision-only PM turn Spec 21 just
   legalised *still* increments `pm_idle` — the run stops the same way it did on
   07-26. A legal turn that does something must count as progress.
2. **Bug #5, self-inflicted.** The exhausted-DEV prompt says *"say so in your
   summary"*; `DeveloperToolPlanIntent` has no `summary` field
   (`extra="ignore"`), and `_corrective_turn_prompt` separately says *"Drop
   unmodeled fields such as summary."* Two correct strings assembling a dead
   end — the fifth unsatisfiable-constraint bug, created while fixing the first
   four. SPEC-25 removes the class; P0.5 removes this instance now.

**Definition of done for Phase 0:** full coding suite green, `errorta status`
unchanged, every new knob at `0` reproduces today's trace, and the two bugs above
have regression tests.

---

## Ownership contract (read this before writing code)

The batch's three hottest files are `autonomy.py`, `runner.py`, and `schemas.py`.
One writer per seam:

| Seam | Owner | Everyone else |
|---|---|---|
| `autonomy.py` — detector **return contract** (`Narrow`/`Escalate`/`Stop`) | **27** | consume it; do not change the type |
| `autonomy.py` — `_intervene` + the loop hook | **23** | 27 *calls* `_intervene` for `Escalate` |
| `autonomy.py` — `publish_detector_state` | **24** | 23/27 read the published snapshot |
| `autonomy.py` — `pm_idle` / progress accounting | **25** | 23 must not also edit it |
| `runner.py` — `governance_state` segment + renderer | **24** | 23's `_last_word_prompt` **calls** this renderer, never reimplements it |
| `runner.py` — `_corrective_turn_prompt` | **25** | — |
| `runner.py` — role seating / `_audit_topology_advisory` | **26** | — |
| `schemas.py` | **25** | nobody else touches it |
| `sidecar.py` / `server.py` / `routes/coding.py` error paths | **22** | — |
| `capabilities.py` / `topology_audit.py` | **26** | — |
| `python/tests/` end-to-end fixture, `pyproject.toml` addopts | **28** | — |

**The one collision that matters:** SPEC-23 and SPEC-27 both restructure how a
detector's result reaches the loop. They are **not parallelisable**. 23 lands
first and installs `_intervene` at the loop call sites with detectors unchanged;
27 then generalises the return type and routes `Escalate` into the same
`_intervene`. Written the other way round, 27 would have nowhere to escalate to.

---

## Dependency graph

```
              P0 prep (one owner)
                     |
        +------------+------------+-------------+
        |            |            |             |
     SPEC-22      SPEC-25      SPEC-26      (parallel, disjoint seams)
   diagnosable  expressibility  role closure
        |            |            |
        +------------+------------+
                     |
                 SPEC-23  (keystone — needs prep; better after 22's traces)
                     |
                 SPEC-24  (enriches 23's intervention prompt)
                     |
                 SPEC-27  (needs 23's _intervene to escalate into)
                     |
                 SPEC-28  (asserts the whole thing; last by construction)
```

## Suggested merge order

| # | Spec | Why here | Can run parallel with |
|---|---|---|---|
| 0 | **P0 prep** | one owner, unblocks everything | — |
| 1 | **22** diagnosable | cheapest; makes every later failure debuggable | 25, 26 |
| 2 | **25** expressibility | highest standalone value; removes the recurring bug class | 22, 26 |
| 3 | **26** role closure | disjoint seams; retires an advisory that fires every run | 22, 25 |
| 4 | **23** continue-by-default | the keystone; wants 22's traces underneath it | — |
| 5 | **24** visibility | enriches 23's prompt; needs the snapshot seam | 26 |
| 6 | **27** convergence-as-control | generalises the contract 23 installed | — |
| 7 | **28** acceptance | the only item that can prove the rest worked | — |

**If you only ship two:** 23 and 25. Together they would have prevented all three
of the 07-26 stops.

---

## Per-spec slicing

Each spec is sliced so every slice is independently mergeable and green.

**22 — diagnosable failures.** S1 sidecar stdio → rotating, redacted log. S2
exception handler + correlation id surfaced by the CLI. S3 transactional create
(no residue on failure). S4 delete clears `cli-team-drafts`. *Ship S1 alone if
nothing else — it is what turns the open `500` into a five-minute fix.*

**25 — expressibility.** S1 `BlockedIntent` + parse dispatch. S2 emit
`task_blocked` from the DEV dead ends (the landing pad already exists in
`autonomy.py`/`topology.py` and is unreachable today). S3 shape-rejection
accounting split from idle. S4 corrective prompts teach a minimal valid example.
S5 the per-role satisfiability invariant test — **that test is the deliverable**;
it is what prevents bug #6.

**26 — role closure.** S1 `role_closure` returning capable/deferred/unclosable.
S2 unseat at seat time + `capability_overrides`. S3 re-evaluate on gate change
and *resolve* the advisory. S4 couple the tester-spawn and merge-gate predicates
so unseating cannot wedge an approved PR (a wedge this spec must fix to be safe).

**23 — continue-by-default.** S1 the HARD/HEURISTIC classification table + its
partition test. S2 `LastWord` action + `_intervene` at **all four** hook sites
(both chains, and both staged-`pending_stop` return points — one of them is easy
to miss and lets a staged stop escape un-intervened). S3 boundedness: per-run
budget, same-detector-once, non-recursion. S4 recording + stop summary.

**24 — visibility.** S1 publish the snapshot from both chains. S2 the renderer +
proximity rule. S3 the prompt segment (absent when nothing is near — golden
lock). S4 point 23's intervention prompt at the same renderer.

**27 — convergence-as-control.** S1 the `DetectorOutcome` contract + loop
application. S2 per-detector ladders. S3 ladder state persistence + reset on
progress. S4 the boundedness proof as tests.

**28 — acceptance.** S1 the fixture + scripted stateful caller. S2 the friction
requirements (a rejected review, a revise, a duplicate task, a context request,
≥12 iterations) with meta-assertions so the fixture cannot go soft. S3 the
stop-reason budget + the three loop-level false-stop locks. S4 `addopts` so
`live`/`flaky`/`manual` cannot enter the merge gate. S5 the browser-backed tier.

---

## Regression locks worth stating up front

Write these before the features; they are the batch's contract with its own past.

1. **No stop-reason string or exit code changes.** `classify_exit` is a
   fail-closed allowlist; `FAILURE_STOP_REASONS` / `SUCCESS_STOP_REASONS` /
   `STOP_REASON_GLOSS` / `_TERMINAL_BAD` need **zero** edits in this batch. A run
   that exhausts its interventions stops exactly as it does today.
2. **Every knob at `0` reproduces today's trace byte-for-byte.**
3. **Hard stops never intervene** — budget, cancel, member-unhealthy-after-ladder
   terminate immediately, unchanged.
4. **Goldens stay byte-identical** when nothing is near a threshold.
5. **Both loop chains, always.** The sequential and concurrent detector chains are
   duplicated; a hook in one is dead code exactly where it matters (Spec 16 shipped
   this bug once — the concurrent loop is where real fanned-out runs live).
6. **`pm_idle_limit` still bounds genuinely empty turns.** Expressibility must not
   become a licence to idle.
7. **The worst case is stated numerically** in each of 23/25/27, and tested.

---

## What this batch does **not** fix

Named so nobody assumes otherwise:

- **The CLI-spawned sidecar `500`.** Root cause still unknown. SPEC-22 makes it
  *diagnosable*, and the traceback turns out to already exist in an in-memory
  `LogBuffer` at `/diagnostics/log-tail` that **no CLI command calls** — so the
  first move after S1 is to read it. Fixing it is its own change.
- **No engine path can register a unit-scoped test command,** so a headless run
  can never arm its own TESTER. SPEC-26 stops lying about the seat; it does not
  create the arm. That is the top follow-up out of this batch.
- **`reviewer_repo_read` defaults `False`,** so the reviewer seat ships ungrounded
  — the configuration the failure report calls a 26–92% false-rejection machine.
  A defaults decision, not a code fix.
- **Spec 20 and Spec 21 shipped without spec docs.** A convention gap worth
  backfilling, off the critical path.
- **A test suite has been writing into the real `${ERRORTA_HOME}`** — 144
  `apply-workspaces` entries across 39 dead project ids, several fixture-shaped.
  Its own bug.
- **Single-agent vs multi-agent.** The failure report found no accuracy edge for
  multi-agent; that is a product question this batch deliberately does not reopen.

---

## Definition of done for the batch

1. SPEC-28 Tier 1 is green in the merge gate: a run reaches `definition_of_done`
   on a buildless-web fixture with real friction, and **never stops on a
   heuristic reason without an intervention**.
2. GL01's `web:probe` has fired in at least one loop-driven test — it never has.
3. The topology advisory either resolves during a run or the role is not seated.
4. Any `500` or turn rejection leaves a traceback and a correlation id.
5. The count of ways to recover is no longer an order of magnitude smaller than
   the count of ways to die.
