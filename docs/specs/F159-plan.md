# F159 — Implementation plan (hot-file serialization)

Spec: [F159-hot-file-serialization.md](F159-hot-file-serialization.md).
This plan reflects the code-grounded review folded into the spec. The **Δ** notes
carry the corrections that reshaped the design; read them before coding — two of
them (merge-scoped hold, WS-D2 reuse) change *what* gets built, not just details.

## Grounding corrections (already folded into the spec)

- **Δ The hold must be merge-scoped, not in-flight-scoped.** The `mockData.ts`
  conflict surfaces at **merge-back**, not during a dev turn. A gate keyed on the
  `in_flight` futures map (`autonomy.py:1074`) only reorders dispatch: dev-1
  finishes → releases the file → dev-2 branches from a stale master → conflict at
  dev-2's merge. So ownership of a hot file must persist from dispatch until the
  owner's **PR merges** (open-PR path ownership). This is the load-bearing fix.
- **Δ WS-D2 already creates the centralize task.** `_contract_owner_for`
  (`runner.py:2075-2151`) auto-creates *"Define + centralize the shared contract
  (types / mock data / component APIs)"*, deduped via
  `run_state.contract_owner_task_id`, on reviewer-finding triggers. F159 **adds a
  conflict-count trigger to it** and a freeze — it does **not** clone
  `_account_foundation_stall`. Reuse the dedup or we spawn a competing owner. (The
  Problem's "the PM created that task" was wrong — it's engine-created.)
- **Δ Touched-files signal is a prerequisite, not optional.** Prose regex
  (`_TARGET_PATH_RE`, `runner.py:481`) misses `mockData.ts` and, when it hits,
  yields a **basename** that won't equal the git **full path**
  (`src/mockData.ts`) in the `conflicts` history — an exact-set intersection
  silently no-ops. So declared `target_files` + a normalization helper are
  Phase 1, and Items 3–4 depend on them.
- **Δ Layering.** `runner` imports `.topology` (`runner.py:43`), so `topology`
  can't top-level import `runner`. Put shared path logic in a new `coding/paths.py`
  (or import function-locally, per `director.py:282`).
- **Δ Cost.** `list_prs()` re-reads/parses `prs.json` each call (`ledger.py:989`).
  `hot_files()` runs **once per iteration**, threaded down — never per dispatch
  candidate.
- **Δ Citations.** Plan-time serializer is `_materialize_pm_tasks`
  (`runner.py:1272`), not `_make_tasks_from_intent`. Policy round-trips via
  `policy_to_dict`/`policy_from_dict` (`autonomy.py:105/123`).
- **Δ Confirmed anchors.** `Task._extras` round-trips (`ledger.py:410/440/445`);
  `run_state` is a mutable JSON dict (`get_run_state`/`set_run_state`,
  `ledger.py:1350/1371`); `workspace.changed_paths` exists + is unstored
  (`workspace.py:331`); merge-success block knows `pr["task_id"]`
  (`runner.py:2803-2828`); `plan_next_batch` gets only idle members
  (`autonomy.py:1030-1031`); merges are forced serial (`autonomy.py:1048-1055`).

## Phase 0 — land the spec + this plan (no code)

Branch `feat/F159-hot-file-serialization`; commit the (reviewed) spec + plan.
Spec-first convention (F151/F157/F158).

## Phase 1 — path helper + touched-files signal (foundation; unit-tested first)

Nothing downstream is reliable without this, so it lands first and standalone.

1. **`coding/paths.py`** — `normalize_path(p)` (→ git repo-relative form),
   `paths_intersect(a_set, b_set)` (exact match on full paths; basename-suffix
   match when one side is a bare filename from prose). Move/duplicate
   `_declared_target_paths` here (drop the runner-only home to kill the cycle).
2. **Declared `target_files`** — plumb an optional `target_files: list[str]`
   through the `create_task` control action (`control_actions.py:321-334`) into
   `store.add_task`, stored in `Task._extras`. Add a one-line PM plan-prompt nudge
   to declare a task's files. Absent → fall back to the prose regex.
3. **Observed `changed_paths`** — persist `workspace.changed_paths(branch)` onto
   the PR record at merge (`record_pr` gains the field, `ledger.py:997`; set in
   the merge block, `runner.py:2817-2824`).
4. **`task_paths(task, pr=None)`** — one resolver: declared > observed (from the
   task's merged PR) > prose regex, returned normalized. Every later phase calls
   this, so contention logic has a single source of truth.

**Tests (`tests/coding/test_f159_hot_file.py`):** `target_files` round-trips;
merged PR records `changed_paths`; `paths_intersect("src/mockData.ts",
"mockData.ts")` is True and rejects a non-match; `task_paths` precedence
(declared > observed > prose).

## Phase 2 — hot-file map (Item 2)

1. `hot_files(ledger, *, threshold) -> dict[str,int]` in `autonomy.py`: tally
   canonical paths across `list_prs()`' `conflicts`. Policy knobs
   `hot_file_threshold=2`, `hot_file_escalation_threshold=4`,
   `hot_file_freeze_stall_limit=15` on `CodingAutonomyPolicy`, round-tripped in
   `policy_to_dict`/`policy_from_dict`.
2. Compute it **once per iteration** in the loop and pass the map down (both the
   sequential and concurrent paths).

**Tests:** counts + threshold classification over a synthetic `list_prs()`;
policy knobs round-trip; assert `hot_files` is invoked once per tick, not per
candidate.

## Phase 3 — merge-scoped ownership gate (Item 3; the core prevention)

1. Extend `_active_dev_path_owners` (`runner.py:797-804`) to include tasks with an
   **open, un-merged PR** (not just `todo`/`doing`), using `task_paths` for the
   path set → `path -> owning_task_id`.
2. Thread `hot_files` + the hot-file owner map into `plan_next_batch`
   (`topology.py:288-391`); in the fan-out (`:360-383`) skip any candidate whose
   `task_paths` include a hot file already owned by an open PR. Wire the owner set
   from the loop (`autonomy.py:1026-1078`).

**Tests (deterministic scheduler):** a hot file owned by an **open, un-merged** PR
blocks a second toucher until merge, while non-colliding tasks still fan out;
**explicitly assert merge-scoped, not in-flight-scoped** (a task whose turn
finished but whose PR is still open keeps the hold — the Δ defect); no hot files →
batch byte-identical to today.

## Phase 4 — history-aware plan-time serialization (Item 4)

Fold the hot-file map into `path_owners` in `_materialize_pm_tasks`
(`runner.py:1289-1293`): a newly-planned task whose `task_paths` include a hot
file gets a `depends_on` edge to the current owner even when prose wouldn't catch
it. Gated on Phase 1's signal (without declared paths, a task that doesn't name
the file can't be chained — and blanket-serializing all new tasks would
over-serialize).

**Tests:** two planned tasks declaring the hot file are chained via `depends_on`;
a cold file is not.

## Phase 5 — conflict-driven centralize + freeze, extending WS-D2 (Item 5)

1. Add a **conflict-count trigger** inside `_contract_owner_for`
   (`runner.py:2075-2151`): when a hot file's count crosses
   `hot_file_escalation_threshold`, invoke the *same* centralize path (reuse
   `run_state.contract_owner_task_id` dedup), record `hot_file_escalated`, raise
   an attention `alert` naming the file.
2. **Freeze** via `run_state.frozen_paths`: Phase-3's gate lets only the
   centralize task touch a frozen file. Seed in `_contract_owner_for`; **lift** in
   the merge-success block when `pr["task_id"]` is the centralize task
   (`runner.py:2803-2828`).
3. **Never-lift escape:** if the freeze persists past
   `hot_file_freeze_stall_limit` iterations, force-lift + raise a distinct
   `hot_file_freeze_stalled` alert (mirror `_account_foundation_stall`'s counter).

**Tests:** threshold crossing → exactly one centralize task (WS-D2 dedup honored)
+ alert + `frozen_paths` set + other touchers blocked; freeze lifts on the
centralize PR merge; a never-merging centralize PR force-lifts + alerts after the
stall limit.

## Phase 6 — integration repro + docs

- **Repro test:** a scripted run with several tasks targeting one mock-data file
  reaches DoD (or a real blocker), **not** `not_converging`; the centralize task
  appears early, not at the iteration cap. This is the acceptance gate for the
  whole feature.
- `docs/CLI.md` + `docs/coding/PM_REFERENCE.md` per the spec.

## Ordering & rollout

Strict dependency chain: **1 → 2 → 3**, with **4** and **5** after 3 (both consume
the map + the ownership gate). Phase 1 is the prerequisite the review surfaced —
do not start 3/4 before it. Suggested: **one PR** for the engine change (commits
per phase for review legibility), Phase 0 the spec/plan commit, Phase 6's repro
test as the merge gate. Because every gate is **opt-in / hot-only**, a run with no
contention is unchanged — low blast radius. Gate: full coding suite (`1720+`) +
`test_f159_hot_file.py`; and a manual `errorta run --autonomous` on a repo with a
shared mock-data file, confirming early centralization instead of churn.

## Risk register

- **Merge-scoped hold regressing throughput (highest).** Holding a hot file until
  its PR merges serializes that path; if "hot" is too aggressive it throttles the
  run. Mitigation: `hot_file_threshold=2` (only after a *real* repeat conflict),
  hot-only scope, and the Phase-3 regression test that a no-hot-file run is
  byte-identical. Tune the threshold from the repro.
- **Never-lifting freeze (deadlock-ish).** A centralize PR that never merges
  freezes the file forever. Mitigation: the Phase-5 stall force-lift + alert;
  tested explicitly.
- **Namespace mismatch silently no-ops the gate.** The exact failure that made
  the original design useless. Mitigation: the Phase-1 `paths_intersect`
  basename-suffix match + its direct test; canonicalize on the git full path.
- **WS-D2 double-owner.** Missing the `contract_owner_task_id` dedup would spawn a
  second centralize task competing with the reviewer-triggered one. Mitigation:
  reuse the existing dedup key; tested.
- **Cost.** `list_prs()` per-candidate would be quadratic. Mitigation: compute
  `hot_files` once per tick; asserted in test.
- **Over-serialization from union'd path signals.** Union of declared ∪ observed ∪
  prose can over-include. Accepted: over-inclusion costs parallelism, never
  correctness; a false-hot path only serializes itself.
