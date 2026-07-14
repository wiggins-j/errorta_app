# F159 — Hot-file serialization: stop parallel writers thrashing a shared file

**Target version:** v0.1 (engine)
**Status:** proposed
**Owner:** wiggins-j

> Feature number is provisional — confirm against the F-registry before merge.
> This revision folds in a code-grounded review (see the **Δ review** notes); the
> original draft mis-scoped the core mechanism (an in-flight gate only reorders a
> conflict that surfaces at *merge*) and missed an existing engine feature (WS-D2
> auto-centralization). Both corrected below.

---

## Problem

A real autonomous run (the reddit-clone demo, `errorta run --autonomous`) built
most of a Next.js app fast, then died with `not_converging` after 159
iterations. The whole tail of the run was a single failure mode: **multiple
parallel dev agents editing one shared file (`mockData.ts`)**. Every
component/page task appended to that central mock-data file, so N parallel
branches touched the same lines → merge conflict at merge-back → the engine
re-dispatched a "resolve conflict" task → the resolve PR *itself* conflicted
against the next merge → `pr_superseded` → a fresh conflict. The team generated
conflicts faster than it could cleanly resolve them; forward progress stalled and
the convergence guard (correctly) stopped the run.

**Where the conflict actually happens (the crux for the fix).** Each dev works on
its own branch/worktree; editing the shared file in a dev turn is *not* itself a
conflict. The conflict materializes only at **merge-back**, when a branch that
edited `mockData.ts` is merged against a master that already moved. So the two
writers don't have to run at the same instant to collide — dev-1 can finish and
open its PR, dev-2 then branches from a master that still lacks dev-1's change,
edits the same file, and the conflict appears when dev-2's PR merges. **Any fix
must hold the file across the open-PR-until-merge lifecycle, not just while a
turn is executing** (Δ review — this sank the original Item 2).

## Why the existing guards didn't catch it

The engine already has *two* relevant mechanisms; the failure is that neither is
driven by the right signal.

1. **Plan-time prose serializer.** `_materialize_pm_tasks` (`runner.py:1272-1338`
   — Δ review: the spec draft mis-named this `_make_tasks_from_intent`) infers
   each new task's target paths from its **title + detail prose** via a regex
   (`_TARGET_PATH_RE`, `runner.py:481-484`; `_declared_target_paths`, `:787-794`),
   looks them up against active DEV tasks' paths (`_active_dev_path_owners`,
   `:797-804`), and injects a `depends_on` edge so two tasks that touch the same
   file **serialize** (`path_deps`, `:1289-1293`). It missed `mockData.ts`
   because a task titled *"Create PostCard component"* never literally names the
   mock-data file, so the collision is invisible to the regex.

2. **WS-D2 reactive contract centralization.** `_contract_owner_for`
   (`runner.py:2075-2151`) **already auto-creates** a task titled *"Define +
   centralize the shared contract (types / mock data / component APIs)"*
   (`runner.py:2096-2097`), deduped via `run_state.contract_owner_task_id`, and
   records a `contract_centralized` decision. (Δ review: the Problem draft
   credited the PM with creating that task — it is **engine**-created.) But WS-D2
   triggers on **reviewer findings** flagging a cross-cutting contract mismatch —
   not on repeated **merge conflicts** — so on this run it fired late / on the
   wrong signal relative to the conflict churn.

Three root gaps, then: (a) **no reliable per-task touched-files signal** — only a
prose regex that misses the file and, when it does match, yields a *basename*
(`mockData.ts`) that won't equal the git *full path* (`src/mockData.ts`) the
conflict history records (Δ review — a silent namespace mismatch); (b) **no
merge-scoped hold** — nothing stops a second task from branch-and-editing a
contended file while the first's PR is still open; (c) **the conflict history
the engine already persists is never fed back** — a file that has conflicted
five times gets no special treatment.

## The analog to build on (F139 foundation clamp)

F159 is the file-scoped sibling of the F139 **foundation-first clamp**: while a
`new` project's foundation hasn't merged, worker concurrency is clamped to 1
(`runtime_cap`, `autonomy.py:184-211`, keyed on `run_state.foundation_status`),
and if it stays clamped too long a `foundation_not_converging` attention signal
fires (`_account_foundation_stall`, `autonomy.py:453-490`). F159 reuses the
shape — an opt-in `run_state` flag + a per-iteration gate + a stall→escalation
path — but scoped to a **hot file**. (Δ review: F139 composes with F159 by
*both* restricting — `runtime_cap` returns a global concurrency number, F159 is a
per-candidate dispatch skip; they are different mechanisms, not a single "min
concurrency". While `foundation_status=="pending"` the cap is already 1, so F159
is a no-op then.)

## Goals

- Give each task a **reliable touched-files signal** (declared by the PM +
  observed from merged diffs), so contention can be detected without relying on
  prose regex.
- **Detect a hot file** — a path parallel dev work keeps colliding on — from the
  conflict history the engine already persists.
- **Hold a hot file to one owner across its open-PR-until-merge lifecycle**, so a
  second writer can't branch-and-edit it in parallel — actually *preventing* the
  conflict, not reordering it.
- **Escalate** a persistently-hot file into the existing WS-D2 centralize task
  **earlier** (conflict-driven) and **freeze** direct parallel edits until it
  lands — with a stall-escape so a never-merging centralize task can't freeze the
  file forever.
- Net: the reddit-clone run reaches Definition-of-Done (or a *real* blocker)
  instead of thrashing to `not_converging` on `mockData.ts`.

## Non-goals

- Not a general git merge-queue / rebase-train, and not a rewrite of conflict
  *resolution* — the existing resolve-task path (`_redispatch_conflict_pr`,
  `runner.py:815-894`) still handles conflicts that slip through.
- Not global throttling — parallelism stays full **except** around hot files. A
  run with no contention behaves exactly as today (opt-in gate, like F139).
- No change to the convergence guard or the stop-reason set. (Δ review confirmed
  `_progress_fingerprint`, `autonomy.py:214-252`, flips on PR churn so
  `not_converging` only trips at the tail — F159 prevents the churn upstream and
  doesn't touch that guard.)
- Not a second centralize mechanism — F159 **extends** WS-D2, reusing its
  `contract_owner_task_id` dedup; it must not spawn a competing owner.

---

## Item 1 — a reliable touched-files signal (the foundation; **core, not optional**)

Δ review promoted this from an optional follow-up to a **prerequisite**: Items 2
and 3 both no-op on the exact repro without it, because prose regex misses
`mockData.ts` and its basename won't match the git full path the conflict history
records. Two sources, cheap and additive:

- **Declared (PM, plan-time).** Add an optional `target_files: list[str]` carried
  in `Task._extras` (`ledger.py:410` — round-trips via `to_dict`/`_split_unknown`,
  `ledger.py:440/445`, **no migration**). Thread it through the `create_task`
  control action (`control_actions.py:321-334`) and `store.add_task`, and bias the
  PM plan prompt to declare the files a task will touch. Falls back to the
  existing `_declared_target_paths` prose regex when absent.
- **Observed (engine, merge-time).** `workspace.changed_paths(branch)`
  (`workspace.py:331-336`) already computes a merged branch's changed files (used
  for diff sizing, `runner.py:1835`) but is **never stored**. Persist it onto the
  PR record at merge (`record_pr` fields, `ledger.py:997-1012`, gain
  `changed_paths`) so the engine has ground-truth of what each merged task
  actually touched — usable to weight *near-collisions*, not just realized
  conflicts.

**Path-namespace normalization (Δ review).** Everything downstream compares paths
as sets, so normalize to one representation. Use the **git repo-relative full
path** as canonical (that's what `conflicts` and `changed_paths` carry); when the
only signal is the prose regex (a basename), match by basename-suffix against the
canonical set. A shared `paths.py` helper (`normalize_path` / `paths_intersect`)
avoids the topology→runner circular import (Δ review: `runner` imports
`.topology` at `runner.py:43`, so topology can't top-level import runner —
function-local import or a shared module, per the `director.py:282` pattern).

**Acceptance.** A task may declare `target_files`; it round-trips through the
ledger. A merged PR records the files it changed. Path comparison matches
`src/mockData.ts` (git) against a `mockData.ts` prose hit. Absent any signal,
behavior is exactly today's (regex-only).

## Item 2 — the hot-file map (the signal)

A per-project map `path -> conflict_count`, computed from data that already
exists: iterate `ledger.list_prs()` (`ledger.py:1040`, returns full PR dicts incl.
the durable, **uncapped** `conflicts` field — Δ review confirmed) and tally each
canonical path appearing in a PR's `conflicts`. A path reaching
`hot_file_threshold` (new policy knob, default **2**) is **hot**.

**Design.** A pure `hot_files(ledger, *, threshold) -> dict[str,int]` in
`autonomy.py`, computed **once per iteration** and threaded down — NOT called
inside the dispatch loop (Δ review: `list_prs()` re-reads + `json.loads`es the
whole `prs.json` each call, `ledger.py:989`; per-candidate calls would be
quadratic). Threshold + escalation threshold are fields on
`CodingAutonomyPolicy` (`autonomy.py:56`), round-tripped in `policy_to_dict` /
`policy_from_dict` (`autonomy.py:105/123` — Δ review: not `to_dict`/`from_dict`).

**Acceptance.** Given PRs whose `conflicts` include `src/mockData.ts` twice,
`hot_files(...)` returns `{"src/mockData.ts": 2, …}`; a once-conflicted file is
not hot; no network, no new storage, computed once per tick.

## Item 3 — merge-scoped hot-file ownership gate (the core prevention)

**This — not an in-flight gate — is what actually prevents the conflict** (Δ
review). While a task that touches a hot file has an **open (un-merged) PR**, that
hot file is *owned*; no other task touching it is dispatched until the owner's PR
**merges** (or is superseded/closed). This closes the branch-from-stale-master
window, so the next writer either waits for the merge (then branches from an
up-to-date master) or is never dispatched concurrently.

**Design.** Extend the existing path-ownership notion. `_active_dev_path_owners`
(`runner.py:797-804`) already maps `path -> task_id` over active DEV tasks;
broaden "active" to include tasks with an **open PR not yet merged**, and use the
Item-1 touched-files signal (declared > observed > prose) for the path set. Thread
`hot_files` + the current hot-file owners into `plan_next_batch`
(`topology.py:288-391`); in the fan-out loop (`topology.py:360-383`) skip a
candidate whose touched paths include a hot file already owned by an open PR
(leave it `todo`; it runs once the owner merges). Merges are already forced serial
(`autonomy.py:1048-1055`), and the concurrent loop calls `plan_next_batch` with
only idle members (`autonomy.py:1030-1031`), so the owner set must be newly
threaded in (it is not derivable from `idle_members`).

**Non-hot files are unaffected** — full parallelism preserved everywhere except a
path that has actually proven contended. A cold file with two concurrent tasks
still runs concurrently (and if it conflicts twice, becomes hot and self-heals).

**Acceptance.** With `src/mockData.ts` hot and task A holding an open PR that
touches it, `plan_next_batch` will not dispatch task B (touching the same file)
until A merges; it hands B's member a non-colliding task meanwhile. No hot files →
dispatch is byte-identical to today (regression-locked).

## Item 4 — history-aware plan-time serialization (strengthen the prose serializer)

Fold the hot-file map into `_materialize_pm_tasks` (`runner.py:1289-1293`) so a
newly-planned task known (via Item 1's declared/observed signal) to touch a hot
file gets a `depends_on` edge to the current owner even when the prose regex would
miss it. (Δ review: this only works with Item 1's signal — the hot-file map says a
file *is* hot, not *which new task* will touch it; without declared paths this
item can't chain "a task that doesn't name the file", and serializing *all* new
tasks through the owner would over-serialize. So Item 4 is explicitly gated on
Item 1.) Belt-and-suspenders with Item 3: plan-time avoids creating the collision;
merge-scoped ownership is the runtime backstop.

**Acceptance.** Once a file is hot, two newly-planned tasks that declare (or are
observed to) touch it are chained rather than left independent.

## Item 5 — conflict-driven centralize + freeze, **extending WS-D2**

When a file stays hot past a higher bar, stop patching and centralize — reusing
the existing machinery, not a clone (Δ review).

**Design.** Add a **conflict-count trigger** to WS-D2's `_contract_owner_for`
(`runner.py:2075-2151`): when a hot file's conflict count crosses
`hot_file_escalation_threshold` (default **4**), invoke the *same* centralize path
(the *"Define + centralize the shared contract…"* task, deduped via
`run_state.contract_owner_task_id`) — so a conflict storm triggers it **early**,
alongside the current reviewer-finding trigger. Then:

1. record a decision (`choice="hot_file_escalated"`) + raise an attention `alert`
   naming the file (mirroring `_account_foundation_stall`'s alert path);
2. **freeze** the file via `run_state.frozen_paths` (a mutable JSON field —
   `get_run_state`/`set_run_state`, `ledger.py:1350/1371`, no migration): until the
   centralize task's PR merges, Item 3's gate lets **only** the centralize task
   touch it. Lift the freeze in the merge-success block, which already knows
   `pr["task_id"]` (`runner.py:2803-2828`, next to `refresh_foundation_status`
   `:2828`).

**Never-lift protection (Δ review).** If the centralize PR itself never merges
(it can conflict against unrelated master movement, or loop in review/tests), the
file would stay frozen forever. Add a `hot_file_freeze_stall_limit` (default,
say, **15** iterations, mirroring `foundation_stall_limit`): if the freeze hasn't
lifted after that many iterations, force-lift it and raise a distinct
`hot_file_freeze_stalled` alert so a human is told, rather than silently starving
the file.

**Acceptance.** A file conflicting 4× triggers the existing centralize task (one,
via the WS-D2 dedup) + an alert naming it; while it's open only the centralize
task touches the file; the freeze lifts on that PR's merge, or force-lifts +
alerts after the stall limit. On the repro, the centralize-mockData task is
created **early** (near the first repeat conflict), not at iteration ~150.

---

## Implementation notes

- **Shared path helper** — new `coding/paths.py` (`normalize_path`,
  `paths_intersect`, basename-suffix match) to dodge the topology→runner cycle;
  `_declared_target_paths` moves here or is called function-locally.
- **Item 1** — `Task._extras["target_files"]`; thread through
  `control_actions.create_task` (`:321`) + `store.add_task`; persist
  `changed_paths` on the PR record at merge (`record_pr`, `ledger.py:997`; set in
  the merge block `runner.py:2817-2824`). PM plan-prompt nudge to declare files.
- **Item 2** — `autonomy.py`: `hot_files(ledger, *, threshold)`; policy knobs
  `hot_file_threshold` (2), `hot_file_escalation_threshold` (4),
  `hot_file_freeze_stall_limit` (15) on `CodingAutonomyPolicy`, round-tripped in
  `policy_to_dict`/`policy_from_dict` (`autonomy.py:105/123`). Compute once per
  tick in the loop.
- **Item 3** — extend `_active_dev_path_owners` (`runner.py:797`) to open-PR
  lifecycle; thread `hot_files` + owner map into `plan_next_batch`
  (`topology.py:288-391`); skip colliding candidates in the fan-out
  (`:360-383`). Wire owner set from the loop (`autonomy.py:1026-1078`).
- **Item 4** — fold the hot-file map into `path_owners` in `_materialize_pm_tasks`
  (`runner.py:1289-1293`).
- **Item 5** — conflict-count trigger inside `_contract_owner_for`
  (`runner.py:2075-2151`, reuse `contract_owner_task_id`); `run_state.frozen_paths`
  seeded there / lifted at `runner.py:2828`; stall force-lift + alert via the
  monitor path.
- No CLI change required — `board`/`prs`/`attention`/`decisions` already surface
  the centralize task, the alert, and the `hot_file_escalated` decision.

## Edge cases

- **A genuinely central file everything imports** (a shared types module):
  Item 5's centralize-and-freeze is exactly right — make it a stable contract once,
  then everyone imports.
- **Centralize PR never merges** → covered by the never-lift stall force-lift +
  `hot_file_freeze_stalled` alert (above).
- **Foundation clamp active** (`foundation_status=="pending"`, cap 1): F159 is a
  no-op; once fanned out, both gates compose (each independently restricts).
- **False-positive hot path** (two incidental conflicts): bounded cost — it only
  *serializes* that one path, never blocks the run; worst case is slightly less
  parallelism on it.
- **Threshold thrash** — the freeze is driven by the escalation counter +
  `frozen_paths` state, not a bare per-tick recompute, so a file hovering at the
  threshold doesn't flip-flop the freeze.
- **A hot file with declared vs observed disagreement** — union the signals
  (declared ∪ observed ∪ prose); over-inclusion only costs parallelism, never
  correctness.
- **`accept` / delivery review** — unaffected; F159 acts during the build loop.

## Testing

- **Item 1**: `target_files` round-trips through `Task._extras`; a merged PR
  records `changed_paths`; the path helper matches `src/mockData.ts` vs a
  `mockData.ts` prose hit and rejects a non-match.
- **Item 2**: `hot_files` over a synthetic `list_prs()` with seeded `conflicts` →
  correct counts + threshold classification; not called per-candidate (assert
  call count / structure).
- **Item 3** (deterministic scheduler test): a hot file owned by an **open,
  un-merged** PR blocks a second toucher from dispatch until merge, while
  non-colliding tasks still fan out; no hot files → batch byte-identical to today
  (regression); explicitly assert the hold is **merge-scoped**, not
  in-flight-scoped (the Δ-review defect — a task that has finished its turn but
  whose PR is still open must still hold the file).
- **Item 4**: two planned tasks declaring the hot file are chained via
  `depends_on`.
- **Item 5**: crossing the escalation threshold triggers exactly one centralize
  task (WS-D2 dedup honored), raises the alert, sets `frozen_paths`, blocks other
  touchers, lifts on the centralize PR merge; a never-merging centralize PR
  force-lifts + raises `hot_file_freeze_stalled` after the stall limit.
- **Integration / the repro**: a scripted run where several tasks target one
  mock-data file reaches DoD (or a real blocker) instead of `not_converging`;
  the centralize task appears early, not at the iteration cap.
- Full coding suite + `ruff`.

## Documentation

- `docs/CLI.md` (observability): the team auto-serializes and then centralizes a
  repeatedly-conflicting shared file, surfaced as a `hot_file_escalated` decision
  + an attention alert.
- `docs/coding/PM_REFERENCE.md`: the new policy knobs (`hot_file_threshold`,
  `hot_file_escalation_threshold`, `hot_file_freeze_stall_limit`), the
  `target_files` task field, and the centralize-and-freeze behavior (as an
  extension of WS-D2).

## Out of scope / follow-ups

- **Predictive** hot-file detection purely from the plan before any conflict —
  needs high-confidence declared `target_files` adoption first (Item 1 is the
  enabler; v1 stays conflict-observed + declared).
- A general **merge-queue** / rebase-train for all PRs.
- Cross-project hot-file learning (a Director-tier memory that "mock-data files
  tend to be hot").
- Auto-splitting a hot file into per-domain modules (beyond one centralize task).
