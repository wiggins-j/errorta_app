# Spec 16 — Implementation plan (revise-chain circuit breaker)

Spec: [SPEC-16-revise-chain-circuit-breaker.md](SPEC-16-revise-chain-circuit-breaker.md).

**Owner:** Engineer B · **Branch:** `feat/spec-16-revise-breaker`
**Base:** `chore/spec-12-18-prep` (merged) · **PR into:** `main`
**Land last of Engineer B's four** — Phase 2 shares the rejection seam with
[Spec 15](SPEC-15-plan.md). No dependency on Engineer A, and (Δ2) **no ordering
constraint against [Spec 18](SPEC-18-plan.md)** now that Spec 18 owns
`_TERMINAL_BAD`'s backfill: this spec only appends its own reason, so the two
touch different lines of `render/status.py`.

The spec's **Δ review** notes that shape this plan:

- The detector chain is **duplicated** (`autonomy.py:1408-1429` sequential,
  `:1759-1780` concurrent). Wiring only the first makes this dead code exactly
  once Spec 13 lifts the clamp and runs go concurrent.
- `finding_class` keyed on "the first blocking finding" breaks against Specs
  14/15 (they flag and suppress findings). Derive it from **all** findings and
  define empty == empty.
- The stop-reason contract needs **four** edits; the "exit-code map" named in the
  original draft does not exist (`classify_exit` already fails closed).

## Phase 0 — spec + plan (no code)

Branch off the merged prep PR; commit the spec + this plan.

## Phase 1 — lineage identity (pure, unit-tested first)

1. **Depth walk.** A helper next to `_supersede_ancestors`
   (`runner.py:677-721`), reusing its traversal and cycle/self guard: follow a
   task's `pr_id` back-link through ancestor PRs' tasks, counting hops.
2. **Terminal set.** Add `blocked` to the walk's break condition
   (`runner.py:697-699`, today `merged`/`abandoned`/`superseded`) — Phase 2
   introduces `blocked`, and without this the walk keeps traversing a PR it just
   retired.
3. **`finding_class`.** Normalized token set over **all** of the rejection's
   findings via `task_dedupe.normalized_tokens` (the same collapse
   `autonomy.py:868-874` uses), equivalently derived from
   `_reason_from_findings` (`runner.py:472-487`). **An empty class compares equal
   to an empty class** — a run of contentless rejections *should* break the
   chain; that is the observed pathology.
4. Persist both as **explicit `add_task` kwargs** (`ledger.py:686-730` — the
   signature is explicit), not post-hoc `_extras` mutation.

**Tests** (`test_spec16_revise_breaker.py`, new): depth over a synthetic 3-deep
lineage; cycle guard terminates; an independent PR starts at 1; a `blocked`
ancestor stops the walk; class normalization collapses two restatements,
separates two genuinely different findings, and treats empty == empty.

## Phase 2 — the breaker

In the shared `_handle_review_rejection` seam (prep PR; originally
`runner.py:3455-3485`) — **outputs side, coordinate with
[Spec 15](SPEC-15-plan.md) Phase 3, which edits the same function**: land 15
first, then rebase.

When `revise_depth >= revise_chain_limit` (default 3) **and** the finding class
equals the previous round's:

1. do **not** create the `revise:` task;
2. mark the PR `blocked` — terminal, and `_set_mergeable_if_ready`'s F104 S6
   guard (`runner.py:2596-2600`) already refuses to resurrect it, so this can
   never become a merge path;
3. create **one** PM re-plan task carrying the verbatim finding, the lineage's PR
   ids, and the repeated class (dedup per lineage, mirroring
   `contract_owner_task_id`, `runner.py:2346`);
4. record `revise_chain_broken` + one deduped alert (reuse `raise_review_alert`,
   `attention.py:703`).

A **different** finding class resets the streak — a lineage working through
successive distinct defects is healthy work and must not be broken.

Apply the same guard at the strict-mode PM-review rejection branch
(`runner.py:3641-3653`), which spawns its own `revise:` task and is otherwise an
unguarded second entrance to the identical spiral.

Clear the breaker state when `_CORRECTIVE_PREFIXES` pruning drops a lineage's
tasks (`runner.py:446`, used at `:646` / `:712`), so a pruned branch leaves no
phantom broken-lineage count.

**Tests.** 3 same-class rejections → one PM task, one decision, one alert, PR
`blocked`, **no 4th revise**; 3 distinct-class rejections → today's behavior,
three revises, no escalation (**the real-progress lock**); the blocked PR is never
`mergeable`; the PM-review path is guarded identically; pruning clears the state.

## Phase 3 — the livelock detector

`_account_revise_livelock(ledger, c, policy)` next to `_account_gate_stall`
(`autonomy.py:818-851`). Signal: the count of broken lineages that have not since
produced a merge; when non-zero and unchanged for `revise_livelock_limit`
iterations (default 5 — i.e. the PM's re-plan did not unstick anything either),
raise the monitor signal and stop `REVISE_LIVELOCK`.

**Wire it into both chains** — `autonomy.py:1408-1429` (sequential) **and**
`:1759-1780` (concurrent), after `_account_gate_stall`, before
`_account_dispatch_wedge`.

**Tests.** Broken lineage + no merge for the limit → `REVISE_LIVELOCK` with a
summary; a merge resets; `0` disables; **the detector fires on the concurrent
loop, not only the sequential one** (the dead-code lock).

## Phase 4 — stop-reason contract (four sites)

1. `REVISE_LIVELOCK` constant beside `GATE_NOT_IMPROVING` / `PLANNING_CHURN` /
   `DISPATCH_WEDGED` (`autonomy.py:51-53`);
2. `FAILURE_STOP_REASONS` (`errorta_cli/runstream.py:66-72`);
3. `STOP_REASON_GLOSS` (`runstream.py:80-102`) — without a gloss the stream ends
   on a bare reason;
4. `_TERMINAL_BAD` (`errorta_cli/render/status.py:26-30`) — **one line, your
   reason only.** Δ2: the set's three pre-existing gaps (`gate_not_improving`,
   `planning_churn`, `dispatch_wedged`) are [Spec 18](SPEC-18-plan.md)'s to
   backfill, since `_TERMINAL_BAD`'s only consumer is the stop-reason styling at
   `render/status.py:68` — Spec 18's own surface. That is what removes the
   circular ordering constraint between these two specs.

No `classify_exit` change: `runstream.py:130-146` is a fail-closed allowlist, so
an unknown reason is already `EXIT_RUN_FAILED`.

**Tests.** All four sites carry the new reason; `classify_exit` is unchanged.

## Phase 5 — the repro + docs

- A scripted run where the reviewer rejects with an identical finding forever
  terminates in escalation → stop, instead of looping to the iteration cap.
  Assert the same fixture loops on `main` — that contrast is the regression lock.
- `docs/coding/PM_REFERENCE.md` — revise chains are bounded; what
  `revise_chain_broken` means and what the PM should do with the escalation.
- `docs/CLI.md` — the `revise_livelock` stop reason and its exit code.

## Definition of done

Full coding suite (`test_dispatch_wedge.py`, `test_gate_stall.py`,
`test_planning_churn.py` neighbours) + `ruff` green. Both locks asserted: the
detector fires on the concurrent loop, and three distinct findings still produce
three revises.
