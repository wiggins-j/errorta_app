# F159 — Hot-file serialization: stop parallel writers thrashing a shared file

**Target version:** v0.1 (engine)
**Status:** proposed
**Owner:** wiggins-j

> Feature number is provisional — confirm against the F-registry before merge.

---

## Problem

A real autonomous run (the reddit-clone demo, `errorta run --autonomous`) built
most of a Next.js app fast, then died with `not_converging` after 159
iterations. The whole tail of the run was a single failure mode: **multiple
parallel dev agents editing one shared file (`mockData.ts`)**. Every
component/page task appended to that central mock-data file, so N parallel
branches touched the same lines → merge conflict → the engine re-dispatched a
"resolve conflict" task → the resolve PR *itself* conflicted against the next
merge → `pr_superseded` → a fresh conflict. The team generated conflicts faster
than it could cleanly resolve them; forward progress stalled and the convergence
guard (correctly) stopped the run.

The PM eventually diagnosed it and created a *"Define + centralize the shared
contract (types / mock data / component APIs)"* task — the right move, but **far
too late** (it's a foundation-first move, not a cleanup move), and the contention
resumed anyway.

## Why the existing guards didn't catch it

The engine already has a plan-time serializer, and it's exactly the thing that
should have prevented this — it just wasn't strong enough:

- `_make_tasks_from_intent` (`runner.py:1272-1338`) infers each new task's target
  paths from its **title + detail prose** via a regex (`_TARGET_PATH_RE`,
  `runner.py:481-484`; `_declared_target_paths`, `:787-794`), looks them up
  against active DEV tasks' paths (`_active_dev_path_owners`, `:797-804`), and
  injects a `depends_on` edge so two tasks that touch the same file **serialize**
  (`path_deps`, `:1289-1293`).
- **Three gaps** let `mockData.ts` through: (1) it's **regex-on-prose** — a task
  titled "Create PostCard component" never literally names `mockData.ts`, so the
  collision is invisible at plan time; (2) it's **plan-time only** — there is no
  dispatch-time gate, so two already-planned independent tasks that both end up
  editing the shared file still run concurrently (`plan_next_batch`,
  `topology.py:360-383`); (3) it's **blind to history** — the engine records the
  exact conflicted paths on every PR (`update_pr(..., conflicts=[...])`,
  `runner.py:2845` / `:753`, durable in `prs.json`) but never feeds that back in,
  so a file that has *already* conflicted five times gets no special treatment.

Meanwhile the failure is invisible to the convergence guard by design:
merge-conflict churn keeps flipping the PR/task fingerprint
(`_progress_fingerprint`, `autonomy.py:214-252`), so `not_converging`
(`convergence_stall_limit`, `autonomy.py:95`) only trips at the very tail once the
resolve tasks stop landing anything net-new — i.e. after a lot of wasted budget.

## The analog to build on (F139 foundation clamp)

This feature is the file-scoped sibling of the existing F139 **foundation-first
clamp**: while a `new` project's foundation hasn't merged, worker concurrency is
clamped to 1 (`runtime_cap`, `autonomy.py:184-211`, keyed on
`run_state.foundation_status`), and if it stays clamped too long a
`foundation_not_converging` attention signal fires (`_account_foundation_stall`,
`autonomy.py:453-490`). F159 reuses that shape: an opt-in signal + a per-iteration
gate + a stall→escalation path — but scoped to a **hot file** instead of the
whole foundation.

## Goals

- **Detect a hot file** — a path that parallel dev work keeps colliding on —
  from the conflict history the engine already persists.
- **Serialize edits to it** so at most one in-flight task touches a hot file at a
  time (a dispatch-time gate, the backstop the plan-time serializer lacks).
- **Escalate** a persistently-hot file into an automatic "centralize the shared
  contract for `<file>`" task + a freeze on direct parallel edits — the move the
  PM made too late, now triggered by the engine early.
- Net effect: the reddit-clone run reaches Definition-of-Done (or a *real*
  blocker) instead of thrashing to `not_converging` on `mockData.ts`.

## Non-goals

- Not a general git merge-queue or a rewrite of conflict *resolution* — F159
  prevents avoidable conflicts; the existing resolve-task path
  (`_redispatch_conflict_pr`, `runner.py:815-894`) still handles the ones that
  slip through.
- Not global throttling — parallelism stays full **except** around hot files.
  A run with no contention behaves exactly as today (opt-in gate, like F139).
- No change to the convergence guard or stop-reason set.
- Not requiring the PM to perfectly predict file layout — the signal is
  observed (realized conflicts), not just declared.

---

## Item 1 — the hot-file map (the signal)

A per-project map `path -> conflict_count`, computed from data that already
exists: iterate `ledger.list_prs()` (`ledger.py:1040`) and tally each path that
appears in a PR's durable `conflicts` field. A path whose count reaches
`hot_file_threshold` (new policy knob, default **2** — a file that has conflicted
twice is contended) is **hot**.

**Design.** A pure helper `hot_files(ledger, *, threshold) -> dict[str,int]` in
`autonomy.py` (next to `_progress_fingerprint`), computed per iteration from
`list_prs()` — no new storage needed for v1 (the PR `conflicts` field is durable;
the capped decision log is *not* used, to avoid lossy history). The threshold is
a field on `CodingAutonomyPolicy` (`autonomy.py:56`), editable mid-run like the
others.

**Acceptance.** Given PRs whose `conflicts` lists include `mockData.ts` twice,
`hot_files(...)` returns `{"mockData.ts": 2, …}`; a file that conflicted once is
not hot; the computation touches no network and no new persistence.

## Item 2 — dispatch-time contention gate (the core fix)

Do not dispatch two worker turns whose known target paths both include the same
hot file. This is the F139-analog: a gate recomputed each iteration at the
dispatch point.

**Design.** Thread the hot-file set and the **in-flight tasks' path sets** into
`plan_next_batch(ledger, idle_members, member_tiers, …)` (`topology.py:288-391`).
In the fan-out loop (`topology.py:360-383`), when considering a candidate task
whose known paths (see Item 4 for how "known" is derived; v1 uses the existing
`_declared_target_paths` inference + any path already recorded on the task)
intersect a hot file that an already-chosen or in-flight task also touches, **skip
it this tick** (leave it `todo`; it runs once the holder finishes). Merges are
already forced serial (`autonomy.py:1048-1055`), so a hot file is only ever
written by one branch at a time → no conflict to resolve. The concurrent loop
must pass the in-flight actions' path sets down (today `plan_next_batch` only
receives idle members — `autonomy.py:1031`); this is the one new wire.

**Acceptance.** With `mockData.ts` hot and dev-1 in-flight on a task touching it,
`plan_next_batch` will not hand dev-2 or dev-3 another task touching
`mockData.ts` in the same tick; it hands them non-colliding tasks instead (full
parallelism preserved for everything else). A run with no hot files dispatches
identically to today (regression-locked).

## Item 3 — history-aware plan-time serialization (strengthen the existing one)

Make the plan-time `depends_on` injection conflict-history-aware, closing the
"regex-on-prose missed it" gap.

**Design.** In `_make_tasks_from_intent` (`runner.py:1289-1293`), in addition to
the current prose-inferred path ownership, consult the Item 1 hot-file map: if a
newly-planned task's inferred paths include a hot file, force a `depends_on` edge
to the current owner of that path even when the regex would otherwise miss the
overlap, and bias the PM's plan prompt to route all edits of a hot file through a
single task. (Belt-and-suspenders with Item 2: plan-time avoids creating the
collision; dispatch-time is the backstop if it's created anyway.)

**Acceptance.** Once `mockData.ts` is hot, two newly-planned tasks that will both
touch it are chained (`depends_on`) rather than left independent, even if neither
title/detail names the file.

## Item 4 — escalate a persistently-hot file to a centralize-and-freeze task

When a file stays hot past a higher bar — it keeps conflicting *despite* Items
2–3 — stop patching and fix the structure, automatically.

**Design.** Model on `_account_foundation_stall` (`autonomy.py:453-490`). Track a
per-hot-file stall counter; when a file's conflict count crosses
`hot_file_escalation_threshold` (default **4**), the engine:

1. records a decision (`choice="hot_file_escalated"`) and raises an **attention
   signal** (kind `alert`, like `foundation_not_converging`) naming the file;
2. creates a single high-priority DEV task *"Centralize `<file>`: define the
   canonical module and have every other task import from it — do not edit
   `<file>` in parallel,"* and
3. **freezes** the file: until that task merges, Item 2's gate treats `<file>` as
   allowing **only** the centralize task to touch it (all other tasks touching it
   wait). This is the F139 clamp applied to one file, driven by
   `run_state` (e.g. `run_state.frozen_paths`), lifted when the centralize task
   merges (mirroring `refresh_foundation_status` at merge, `runner.py:2828`).

**Acceptance.** A file that conflicts 4× triggers the centralize task + an
attention alert naming it; while that task is open, no other task touching the
file is dispatched; when it merges, the freeze lifts and normal (Item-2-gated)
parallelism resumes. On the reddit-clone repro, the centralize-mockData task is
created **early** (near the first repeat conflict) instead of at iteration ~150.

## Item 5 — first-class touched-files + persisted merged changes (reduce prose reliance) — OPTIONAL / follow-up

The above still leans on prose inference for "which files will this task touch."
Two instrumentation upgrades make it robust; both are additive and can land
after the MVP (Items 1–4):

- **Declared touched-files on a task.** Add an optional `target_files: list[str]`
  carried in `Task._extras` (`ledger.py:410` — no schema migration) and let the
  PM populate it via the `create_task` control action (`control_actions.py:321`).
  Item 2/3 prefer the declared set over the regex when present.
- **Persist merged-PR changed files.** `workspace.changed_paths(branch)`
  (`workspace.py:331-336`) already exists and is used for diff sizing but is
  never stored. Write it onto the PR record at merge (`runner.py:2817-2824`) so
  the hot-file map can also weight *near-collisions* (two tasks that changed the
  same file in quick succession), not only realized conflicts — earlier warning.

**Acceptance.** With `target_files` declared, serialization no longer depends on
the file being named in prose; with merged changed-paths persisted, a file can be
flagged hot before it produces its second *conflict*.

---

## Implementation notes

- **Item 1** — `autonomy.py`: `hot_files(ledger, *, threshold)` + policy fields
  `hot_file_threshold` (2) / `hot_file_escalation_threshold` (4) on
  `CodingAutonomyPolicy` (round-trip in `to_dict`/`from_dict` like the others,
  `autonomy.py:118,147`).
- **Item 2** — `topology.py:plan_next_batch`: accept `hot_files` + `busy_paths`
  (union of in-flight tasks' known paths); skip a candidate whose known paths
  collide on a hot file. `autonomy.py:_run_concurrent_loop` (~`:1026-1078`):
  compute the hot-file map once per tick and thread the in-flight path sets down.
  Reuse `_declared_target_paths` (`runner.py:787`) for "known paths" until Item 5.
- **Item 3** — `runner.py:_make_tasks_from_intent` (`:1289-1293`): fold the
  hot-file map into `path_owners` so a hot path forces a dependency edge.
- **Item 4** — a `_account_hot_file_stall`-style helper mirroring
  `_account_foundation_stall` (`autonomy.py:453`); `run_state.frozen_paths`
  seeded/lifted at merge (mirror `refresh_foundation_status`, `runner.py:2047`,
  `:2828`); the centralize task via `store.add_task` with a `depends_on`-free
  high-priority slot; attention signal via the existing monitor path.
- **Item 5** — `Task._extras["target_files"]`; `create_task` schema in
  `control_actions.py`; PR `changed_paths` persisted at merge.
- No CLI change required — the existing `board`/`prs`/`attention`/`decisions`
  views already surface the new task, the alert, and the `hot_file_escalated`
  decision. (A `--verbosity`-gated "serialized around hot file X" log line is a
  nice-to-have, not required.)

## Edge cases

- **A genuinely central file that everything imports** (a shared types module):
  Item 4's centralize-and-freeze is exactly right — make it a stable contract
  once, then everyone imports. The freeze must lift promptly on merge or it
  serializes the whole run; tie it strictly to the centralize task's PR merging.
- **Foundation clamp already active:** while `foundation_status=="pending"` the
  cap is already 1, so F159 is a no-op then; it only bites once the team has
  fanned out. The two gates compose (take the min concurrency).
- **Hot file with only one eligible owner role** (e.g. only DEV touches it):
  fine — non-colliding tasks for other roles still dispatch.
- **Threshold thrash:** a file oscillating just under/over the threshold
  shouldn't flip-flop the freeze each tick — the freeze is driven by the
  escalation counter + `frozen_paths` state, not a bare per-tick recompute.
- **False-positive hot path** (two conflicts that were incidental): capped cost —
  it only *serializes* that file, never blocks the run; worst case is slightly
  less parallelism on one path.
- **`accept`/delivery review** are unaffected — F159 acts during the build loop.

## Testing

- **Item 1**: `hot_files` over a synthetic `list_prs()` with seeded `conflicts`
  lists → correct counts + threshold classification; capped decision log is not
  consulted.
- **Item 2** (deterministic scheduler test): given a hot file and an in-flight
  task touching it, `plan_next_batch` never returns a second task touching it in
  the same tick, but still fans out non-colliding tasks; with no hot files the
  batch is byte-identical to today (regression).
- **Item 3**: two planned tasks inferred to touch a hot file are chained via
  `depends_on` even when the file isn't named in prose (inject it into the map).
- **Item 4**: a file crossing the escalation threshold creates exactly one
  centralize task, raises the alert, sets `frozen_paths`, blocks other touchers,
  and lifts on the centralize PR's merge.
- **Integration / the repro**: a scripted run where several tasks all target one
  mock-data file reaches DoD (or a real blocker) instead of `not_converging`;
  assert the centralize task appears early, not at the iteration cap.
- Full coding suite + `ruff`.

## Documentation

- `docs/CLI.md` (runtime/observability): note that the team auto-serializes and
  then centralizes a repeatedly-conflicting shared file, surfaced as a
  `hot_file_escalated` decision + an attention alert.
- `docs/coding/PM_REFERENCE.md`: document the new policy knobs
  (`hot_file_threshold`, `hot_file_escalation_threshold`) and the
  centralize-and-freeze behavior.

## Out of scope / follow-ups

- **Predictive** hot-file detection from the PM's plan before any conflict
  occurs (needs reliable declared `target_files` — Item 5) — v1 is
  conflict-observed + prose-inferred.
- A general **merge-queue** / rebase-train for all PRs (bigger scheduler change).
- Cross-project hot-file learning (a Director-tier memory that "mock-data files
  are usually hot").
- Auto-splitting a hot file into per-domain modules (beyond a single centralize
  task).
