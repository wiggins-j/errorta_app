# Spec 16 — Revise-chain circuit breaker

**Source:** `docs/coding/RUN_ANALYSIS_GRAVITY_GOLF_2026-07-24.md` §6 **S5 (P1)**
**Target version:** v0.1 (engine)
**Status:** proposed (revised after a code-grounded review — see the **Δ review** notes)
**Owner:** wiggins-j

---

## Problem

By 17:45 the run was working on `revise: task-t-6ad32880fe5d` — a revise of a
revise of a revise — on ~2-minute cycles, one dev, the reviewer rejecting each
round with the same finding class. Nothing stopped it. Nothing was going to stop
it short of a budget cap.

This is a **capability wedge**: a livelock in which tasks keep completing, so
every wedge detector reads green while the run makes no progress.

## Why every existing guard is blind to it

Four detectors, all structurally unable to see this shape:

- **`_progress_fingerprint`** (`autonomy.py:364-395`) — its own docstring says a
  task-set change counts as motion, deliberately, to fix the "productive PM
  planning stalls the run" false-fire. A revise chain adds a task and completes a
  task every cycle, so `not_converging` never fires. The docstring even names the
  division of labour: reddit-style busy churn "keeps changing this fingerprint,
  so WS-E does NOT stop it".
- **`_account_gate_stall`** (`autonomy.py:818-851`) — keys on
  `_gate_fingerprint` (`:405-440`), which returns the no-signal sentinel
  `((), -1)` with no test runs. In this run there were none
  (see [Spec 12](SPEC-12-in-loop-acceptance-gate.md)), so it could not trip.
- **`_account_dispatch_wedge`** (`autonomy.py:968-1006`) — requires
  `wedge_min_tasks` todo **and nothing dispatchable**. Here exactly one task was
  always dispatchable. Its docstring is explicit that it targets the opposite
  pathology (130 todo, zero dispatchable).
- **`task_reassignment_limit`** (`autonomy.py`, F127 ladder) — scoped to *one*
  task. Every revise is a **new** task, so the counter resets each round.

Meanwhile the data needed to detect it is already persisted. Each `revise:` task
carries a `pr_id` back-link to the PR it supersedes (`runner.py:3474-3477`,
F091), and `_supersede_ancestors` (`runner.py:677-712`) already walks that chain
at merge time. The lineage is a first-class, walkable structure that no detector
reads.

## Goals

- Recognize a **revise lineage** and measure its depth and its repeated
  finding class.
- At a bounded depth, **stop spawning revises** and escalate to a PM re-plan turn
  with the finding attached, instead of handing the same rejection back to the
  same role.
- Make the livelock **visible to wedge detection** — a run that cannot escape the
  escalation must stop with a named reason rather than burning to the iteration
  cap.

## Non-goals

- Not overriding the reviewer. A rejection stays a rejection; the breaker changes
  **who is asked to solve it** after N identical rounds.
- Not auto-merging the rejected PR. Ever.
- Not deduping findings across unrelated PRs — the breaker is strictly
  lineage-scoped (same discipline as `_supersede_ancestors`: follow only this
  PR's own chain, never a shared-key query, so an independent PR can never be
  swept in).
- Not a replacement for [Spec 15](SPEC-15-capability-aware-planning.md). That
  spec prevents the *impossible* findings; this one bounds *any* repeated
  finding, including legitimate ones the dev simply keeps failing to address.

---

## Item 1 — Lineage identity, depth, and finding class

**Design.** Two derived values, computed from data already on the ledger:

- **`revise_depth`** — walk the `pr_id` back-link chain from a task through its
  ancestor PRs' tasks (the exact traversal `_supersede_ancestors` performs,
  `runner.py:687-700`, including its cycle/self guard), counting hops. Store the
  computed depth on the new revise task's `_extras` (`ledger.py:410`, round-trips
  via `to_dict` / `_split_unknown`, no migration) so it is O(1) to read and the
  walk happens once, at creation.
- **`finding_class`** — the normalized token set of the rejection's findings via
  `task_dedupe.normalized_tokens` (already used to collapse restated titles,
  `autonomy.py:868-874`). "No evidence tests were run" and "no evidence that the
  tests were actually run" collapse to one class, which is exactly the collapse
  this detector needs. Stored on the task at creation.

  **Δ review — compute it over *all* findings, not the first blocking one, and
  define the empty case.** [Spec 14](SPEC-14-grounded-reviewer.md) Item 3 flags
  uncited findings and [Spec 15](SPEC-15-capability-aware-planning.md) Item 3
  suppresses some rejections entirely, so keying on "the first blocking finding"
  would either read an absent value every round (the breaker never fires and the
  livelock survives this spec) or compare empty-to-empty and fire on round two.
  So: derive the class from the full finding set (equivalently, from
  `_reason_from_findings`, `runner.py:472-487`), and state explicitly that an
  **empty class compares equal to an empty class** — a run of contentless
  rejections *should* break the chain. That is the observed pathology.

**Acceptance.** A third-generation revise task carries `revise_depth == 3` and
the finding class of its rejection. An independent PR's revise starts at 1. A
cyclic/self back-link terminates the walk (no infinite loop).

## Item 2 — The breaker: escalate instead of respawning

**Design.** At the reviewer-rejection branch (`runner.py:3459-3484`), before
creating the `revise:` task:

- if `revise_depth >= revise_chain_limit` (policy, default **3**) **and** the
  finding class equals the previous round's, do **not** create the revise task.
  Instead:
  1. mark the PR `blocked` (a terminal state the merge gate already refuses to
     resurrect — `_set_mergeable_if_ready`'s F104 S6 guard, `runner.py:2596-2600`
     — so this cannot accidentally become a merge path). **Δ review: add
     `blocked` to `_supersede_ancestors`' terminal set** (`runner.py:697-699`,
     today `merged`/`abandoned`/`superseded`), or the lineage walk keeps
     traversing a PR this item just retired;
  2. create **one** PM re-plan task carrying the verbatim finding, the lineage's
     PR ids, and the repeated class, with `reason_summary` from
     `_reason_from_findings` (`runner.py:472-487`);
  3. record a decision (`choice="revise_chain_broken"`) and raise a deduped
     non-blocking Alert naming the branch and the class.

A **different** finding class resets the streak — a lineage making real progress
through successive distinct defects is healthy work, not a livelock, and must not
be broken.

The dedup for the PM task mirrors the WS-D2 `contract_owner_task_id` pattern
(`runner.py:2346`): one escalation per lineage, not one per iteration.

**Acceptance.** Three consecutive rejections of the same class on one lineage
produce exactly one PM re-plan task, one decision, one alert, a `blocked` PR, and
**no** fourth `revise:` task. Three rejections with distinct classes produce
today's behavior (three revises, no escalation). The blocked PR never becomes
mergeable.

## Item 3 — Livelock visible to the loop

**Design.** A new detector `_account_revise_livelock(ledger, c, policy)` in
`autonomy.py`, wired after `_account_gate_stall` and before
`_account_dispatch_wedge` in **both** per-iteration detector chains: the
sequential loop (`autonomy.py:1408-1429`) **and** the concurrent loop
(`autonomy.py:1759-1780`).

**Δ review — wiring only the sequential chain would make this dead code exactly
where it is needed.** The chain is duplicated, and once
[Spec 13](SPEC-13-foundation-gate-buildless-web.md) lifts the foundation clamp
`runtime_cap > 1` puts real runs on `_run_concurrent_loop`. A detector present
only in the sequential path would never execute on a fanned-out run.

Signal: the count of **broken lineages** (Item 2's decisions) that have not since
produced a merge. When that count is non-zero and unchanged for
`revise_livelock_limit` iterations (policy, default **5**) — i.e. the PM's
re-plan did not unstick anything either — raise the monitor signal and stop with
`REVISE_LIVELOCK`.

**Stop-reason contract impact (call this out explicitly).** F147 §2 makes
stop-reasons a **stable exit-code contract**. Adding `revise_livelock` requires
**four** coordinated edits (Δ review — the original said three, and named a map
that does not exist):

1. the constant next to `GATE_NOT_IMPROVING` / `PLANNING_CHURN` /
   `DISPATCH_WEDGED` (`autonomy.py:51-53`);
2. `FAILURE_STOP_REASONS` (`errorta_cli/runstream.py:66-72`);
3. `STOP_REASON_GLOSS` (`errorta_cli/runstream.py:80-102`) — without a gloss the
   stream ends with a bare reason;
4. `_TERMINAL_BAD` (`render/status.py:26-30`).

**No exit-code map change:** `classify_exit` (`runstream.py:130-146`) is a
fail-closed allowlist — anything not in `SUCCESS_STOP_REASONS`
(`runstream.py:75-78`) is already `EXIT_RUN_FAILED`. And note `_TERMINAL_BAD`
is currently **missing** `gate_not_improving`, `planning_churn`, and
`dispatch_wedged` — Specs 04/07/10 skipped it. Fold those three in with this
change so the set is finally correct rather than newly inconsistent.

**Acceptance.** A run with one broken lineage and no subsequent merge for 5
iterations stops `revise_livelock` with a summary naming the lineage; a run whose
PM re-plan produces a merge resets and does not stop. `revise_livelock_limit ==
0` disables the detector (matching the `max(0, …)` clamp convention Spec 04 and
Spec 10 established, `autonomy.py:218-231`). `errorta status` renders the reason
in the failure style.

---

## Implementation notes

- **`ledger.py`** — `revise_depth` / `finding_class` as **explicit `add_task`
  kwargs** (`:686-730`; the signature is explicit) rather than post-hoc `_extras`
  mutation (`Task._extras`, `:418`). Additive, no migration.
- **`runner.py`** — lineage walk helper next to `_supersede_ancestors`
  (`:677-721`), reusing its traversal and cycle guard, and adding `blocked` to
  its terminal set (`:697-699`); breaker in the shared rejection seam
  (`:3455-3485`); the same guard on the strict-mode PM-review rejection branch
  (`:3641-3653`), which spawns its own `revise:` task and would otherwise be an
  unguarded second entrance to the identical spiral.
- **`autonomy.py`** — `REVISE_LIVELOCK` constant (`:51-53`);
  `_account_revise_livelock` next to `_account_gate_stall` (`:818-851`); wired
  into **both** chains (`:1408-1429`, `:1759-1780`). The policy fields
  `revise_chain_limit` (3) / `revise_livelock_limit` (5) land in the shared prep
  PR with the `max(0, …)` disable convention.
- **`errorta_cli`** — `FAILURE_STOP_REASONS` + `STOP_REASON_GLOSS`
  (`runstream.py:66-72`, `:80-102`); `_TERMINAL_BAD` (`render/status.py:26-30`),
  folding in the three missing existing reasons. **Sequence this before
  [Spec 18](SPEC-18-cli-status-unbound-directory.md)** — both touch
  `render/status.py`.
- **`attention.py`** — reuse `raise_review_alert` (`:703`) for the breaker alert.

## Edge cases

- **A legitimately hard defect** the dev needs 4 rounds for: the breaker fires at
  3 and hands it to the PM, which can re-scope or split it — a *better* outcome
  than a 4th identical round, and the PM can re-plan the same work if it judges
  that right.
- **A distinct finding each round** (real progress): never breaks. This is the
  designed escape hatch and the reason the class comparison exists.
- **The PM re-plan also fails**: Item 3 stops the run with a named reason instead
  of silence.
- **Interaction with F159 WS-D2 contract centralization** (`runner.py:2346`): a
  contract-mismatch class already routes to a centralize task and the revise
  waits on it. The breaker only fires when that mechanism *hasn't* resolved the
  class after N rounds — the two compose, and the centralize task's own lineage
  is measured like any other.
- **A superseded ancestor** in the chain: `_supersede_ancestors` marks prior PRs
  terminal at merge and **breaks** the walk at `merged`/`abandoned`/`superseded`
  (`runner.py:697-699`), so a merged lineage never counts as stuck — once
  `blocked` joins that set (Item 2), a broken lineage stops being walked too.
- **Corrective-task pruning** (`_CORRECTIVE_PREFIXES`, `runner.py:446`, used at
  `:646` / `:712`) drops `revise:` tasks for dead branches — a lineage whose
  branch is pruned must not leave a phantom broken-lineage count. Clear the
  lineage's breaker state there.

## Testing

- **Item 1**: depth walk over a synthetic 3-deep lineage; cycle guard
  terminates; independent PR starts at 1; a `blocked` ancestor stops the walk;
  finding-class normalization collapses two restatements of the same title,
  separates two genuinely different ones, and treats **empty == empty** (the
  contentless-rejection case).
- **Item 2**: 3 same-class rejections → one PM task, one decision, one alert,
  PR blocked, no 4th revise; 3 distinct-class rejections → today's behavior
  (regression lock); the blocked PR is never `mergeable`; the PM-review rejection
  path is guarded identically.
- **Item 3**: broken lineage + no merge for `revise_livelock_limit` iterations →
  `REVISE_LIVELOCK` with a summary; a merge resets; `0` disables. **Assert the
  detector fires on the concurrent loop, not only the sequential one** — that is
  the dead-code lock. `FAILURE_STOP_REASONS`, `STOP_REASON_GLOSS` and
  `_TERMINAL_BAD` all carry the new reason (and the three previously-missing
  ones).
- **The repro**: a scripted run where the reviewer rejects with an identical
  finding forever terminates in escalation → stop, instead of looping to the
  iteration cap. Under today's code the same fixture loops; that is the
  regression lock.
- Full coding suite (`test_dispatch_wedge.py`, `test_gate_stall.py`,
  `test_planning_churn.py` neighbours) + `ruff`.

## Documentation

- `docs/coding/PM_REFERENCE.md`: revise chains are bounded; what
  `revise_chain_broken` means and what the PM is expected to do with the
  escalation; the two new knobs.
- `docs/CLI.md`: the new `revise_livelock` stop reason and its exit code.

## Out of scope / follow-ups

- Cross-lineage finding analysis ("this class of finding recurs across the whole
  project — the spec is wrong").
- Automatically re-scoping the task at break time instead of asking the PM.
- Escalating the *reviewer* (a second opinion on a finding the dev keeps
  failing to satisfy) — interesting, and it needs
  [Spec 14](SPEC-14-grounded-reviewer.md) to land first so a second opinion is
  worth more than the first.
