# F157 — Implementation plan (runtime orphan reaping + safe delete)

Spec: [F157-runtime-orphan-reaping-and-safe-delete.md](F157-runtime-orphan-reaping-and-safe-delete.md).
This plan reflects a grounding pass over the code; where it refines the spec, the
**Δ** notes say how.

## Grounding corrections (fold into the spec)

- **Δ G2 rmtree locations.** The tree removal that races a live server is
  `ApplyWorkspace.destroy()` → `shutil.rmtree(self._root)`
  (`errorta_tools/runner/apply_workspace.py:445`), reached via
  `CodingWorkspace.destroy()` (`workspace.py:118`). A **second** unguarded
  `shutil.rmtree(root)` is in `LedgerStore.delete_project()` (`ledger.py:667`).
  `ApplyWorkspace.destroy` also calls `_clear_owned_worktrees()` (git worktree
  remove) first, which can fail on a busy worktree. All three need hardening.
- **Δ Ownership guard uses psutil.** `psutil>=6.0` is already a dep
  (`python/pyproject.toml:18`; used at `runtime_process.py:1346`). Use
  `psutil.Process(leader).cwd()` / `.cmdline()` for the PID-reuse guard — no
  `lsof`. `RuntimeSession` persists **no** cwd/argv (`runtime.py:118-133`), so
  "expected" is derived from `project_id` (deterministic apply-workspace path) and
  the profile's `start` argv.
- **Δ Boot reap home.** Add to the existing *"coding recovery at startup"* block
  in the lifespan (`server.py:260`), before `yield`, mirroring the shutdown
  `teardown_all()` at `server.py:381`.
- **Δ Access helpers exist.** Reuse `RuntimeProfileStore.for_ledger(store)`
  (`runtime.py:275`) / `_runtime_store(project_id)` (`coding.py:3128`);
  `list_projects()` (`ledger.py:509`) for the boot-wide sweep.

## Phase 0 — land the spec (no code)

Branch `feat/F157-runtime-orphan-reaping`, commit the spec + this plan. Keeps the
F152–F156 convention (spec-first, reviewed on its own).

## Phase 1 — reap primitives in `runtime_process.py` (pure, unit-tested first)

The foundation everything else calls. No callers wired yet, so it lands safe.

1. **Factor `_kill_pgid(pgid, *, grace)`** out of `_kill_group` (`:1458-1478`):
   move the `os.killpg(TERM)` → grace → `os.killpg(KILL)` body to take a bare
   pgid; `_kill_group(live, …)` becomes `_kill_pgid(live.pgid, grace=…)`. No
   behavior change (regression-guard the existing launch/teardown tests).
2. **`_pgid_is_ours(pgid, *, workspace_root, start_argv) -> bool`** — the
   PID-reuse guard. `psutil.Process(pgid)` (pgid == group-leader pid for our
   session-leader spawns); return `True` only if the leader is alive **and**
   (`proc.cwd()` is within `workspace_root`) or (`proc.cmdline()` prefix-matches
   `start_argv`). Any `psutil` error / no-match ⇒ `False` (never kill an
   unconfirmed group). Belt-and-suspenders: optionally also require one of
   `sess.allocated_ports` still bound by the group.
3. **`reap_persisted_sessions(rstore, *, project_id=None) -> int`** — iterate
   `rstore.list_sessions()`; skip `state in _TERMINAL` (`:51`) or `pgid is None`;
   for the rest resolve `workspace_root` + `start_argv` from the profile
   (`rstore.get_profile(sess.profile_id)`); if `_pgid_is_ours` → `_kill_pgid` +
   `update_session(state="stopped", error="reaped_orphan")`; else →
   `update_session(state="stopped", error="orphan_gone")` (dead/foreign, record
   only). Return kill count.
4. **`reap_all_persisted_orphans() -> int`** — `for p in list_projects():`
   build `RuntimeProfileStore.for_ledger(LedgerStore(p["id"]))`, sum
   `reap_persisted_sessions(rstore)`. Defensive per-project try/except so one bad
   store can't abort the sweep. Export via `__all__` (joins `teardown_all`,
   `teardown_project`).

**Tests (`tests/coding/test_f157_orphan_reap.py`)** — spawn a real detached child
in a temp workspace, write a matching persisted session, then:
`test_reap_persisted_orphan_kills_group`,
`test_reap_skips_terminal_and_missing`,
`test_reap_ownership_guard_spares_foreign_pgid` (safety-critical: session pgid →
a process with a mismatched cwd/argv is left alive),
`test_reap_all_iterates_projects`. Plus `_kill_pgid` regression via existing
launch tests.

## Phase 2 — delete reaps first + resilient teardown (fixes G1 + G2)

1. **`routes/coding.py:590-611`**, inside `with store.lock:` before `ws.destroy()`:
   ```python
   from errorta_council.coding import runtime_process as _runtime
   _runtime.teardown_project(project_id)                 # this sidecar's live servers
   _runtime.reap_persisted_sessions(RuntimeProfileStore.for_ledger(store),
                                    project_id=project_id)  # cross-restart orphans
   ```
   (reuse the module-level `RuntimeProfileStore` import already at `coding.py:355`).
2. **Resilient removal (G2).** Add a small `_rmtree_resilient(path)` helper
   (chmod-and-retry `onerror`, bounded 3× with short backoff) and use it in
   `ApplyWorkspace.destroy` (`apply_workspace.py:445`) and
   `LedgerStore.delete_project` (`ledger.py:667`). Guard `_clear_owned_worktrees`
   with `git worktree remove --force` fallback. After Phase-2 step 1 the tree is
   already quiescent; this is the belt-and-suspenders for a slow-dying child.

**Tests (extend `tests/coding/`):**
`test_delete_reaps_running_server` (spawn real server → delete → group dead +
tree gone: the end-to-end repro of the live 500),
`test_delete_survives_open_worktree` (a child writing into the tree during delete
⇒ `{"deleted": True}`, no raise).

## Phase 3 — boot-time reap (fixes G3, the root cause)

In `server.py` lifespan, in/after the *"coding recovery at startup"* block
(`:260`), before `yield`:
```python
# F157: reap managed-local runtime servers orphaned by a NON-graceful prior exit
# (shutdown teardown_all only runs on a clean exit; a crash/SIGKILL leaks them).
try:
    from errorta_council.coding import runtime_process as _runtime
    n = _runtime.reap_all_persisted_orphans()
    if n:
        log.info("F157: reaped %d orphaned runtime server(s) from a prior sidecar", n)
except Exception:               # never block startup on reaping
    log.debug("F157 boot reap skipped", exc_info=True)
```

**Test:** `test_boot_reap_clears_prior_orphans` — populate a store with a live-child
session, ensure `_LIVE` is empty (simulating a fresh process), call
`reap_all_persisted_orphans()`, assert the child is dead and the session is
`stopped/reaped_orphan`.

## Phase 4 — docs + surfacing

- `docs/CLI.md`: delete stops the project's managed-local servers first; the
  sidecar reaps orphaned runtime servers on startup (no bound-port leak across a
  crash).
- `docs/coding/PM_REFERENCE.md`: add `reaped_orphan` / `orphan_gone` if session
  error markers are enumerated.
- Update the F157 spec with the four **Δ** corrections above.

## Ordering & rollout

Phases are independently landable and each is safe on its own: **1** adds unused
primitives, **2** fixes the delete papercut, **3** closes the root leak, **4** is
docs. Recommend a single PR (they're one coherent fix) with commits per phase for
review legibility. Gate: full coding suite (`1720+` currently) + the new
`test_f157_*`, and a manual repro — spawn a dev server via `errorta runtime run`,
`kill -9` the sidecar, restart, confirm the boot reap kills the orphan.

## Risk register

- **PID reuse (highest).** Mitigated by `_pgid_is_ours`; the foreign-pgid test is
  the gate. Fail closed — an unconfirmed group is never killed.
- **psutil quirks** (zombie leader, permission). All psutil calls in the guard
  wrapped; any error ⇒ "not ours" ⇒ no kill (safe default, worst case a leak
  persists rather than a wrong-kill).
- **Killing a group the operator started** (imported real repo). Out of scope by
  construction — only sessions we persisted carry a pgid; nothing else is a
  target.
- **Boot latency.** `reap_all_persisted_orphans` is O(projects × sessions) of
  cheap store reads + a psutil probe per non-terminal session; bounded and inside
  a try that never blocks `yield`.
