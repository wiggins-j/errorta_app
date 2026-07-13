# F157 — Runtime orphan reaping + safe project delete

## Problem

Observed live while deleting a finished project (`reddit-clone`): two `next dev`
server trees from the previous evening were **still running**, `errorta delete`
returned a **500**, and after the delete "succeeded" the workspace directory kept
**reappearing** on disk. All three symptoms share one root cause — managed-local
runtime processes are only tracked in memory, so nothing reaps them across a
sidecar restart, and `delete` never asks them to stop before removing their
working tree.

Three grounded gaps:

### G1 — `delete_project` never reaps the project's runtime

`delete_project` (`errorta_app/routes/coding.py:590-611`) does:

```python
ws = CodingWorkspace(project_id, store)
ws.set_target(proj.target)
with store.lock:
    ...
    ws.destroy()            # <-- rmtree the worktree
    store.delete_project()
    _RUNS.pop(project_id, None)   # <-- this is the COUNCIL run thread, not runtime servers
```

`_RUNS.pop` drops the council loop thread. It does **not** touch managed-local
runtime preview processes. The teardown function built for exactly this —
`teardown_project(project_id)` (`runtime_process.py:1517`) — is **never called**
on the delete path (grep-confirmed). So a live `next dev` / `vite` / `uvicorn`
the project launched keeps running with its `cwd` inside the worktree that is
about to be deleted.

### G2 — `ws.destroy()` races a live writer → 500

`CodingWorkspace.destroy()` (`workspace.py:118-121`) delegates to an unguarded
`rmtree` of the worktree. With a dev server still writing `.next` into that tree
(G1), the `rmtree` hits files that vanish/recreate under it and raises — surfaced
to the CLI as `sidecar returned 500`. Reproduced exactly: the first
`errorta delete reddit-clone` 500'd; only after the dev servers were killed by
hand did destroy succeed. Even with G1 fixed, destroy should tolerate a
briefly-open tree rather than fail the whole delete.

### G3 (root cause) — runtime liveness is in-memory only; no reap survives a restart

`_LIVE` (`runtime_process.py:199`, populated at `:638/:752/:827/:1012`) is a
plain in-process dict of `_Live` records. Both teardown entry points read only
`_LIVE`:

- `teardown_all()` (`:1507`) — called **only** from the sidecar shutdown `finally`
  block (`server.py:381`, "so no generated dev server / bound port leaks across a
  restart", F101 D3).
- `teardown_project()` (`:1517`) — same `_LIVE`-only scope.

So the leak has two escape hatches:

1. **Non-graceful exit.** If the sidecar is `SIGKILL`ed / crashes / is force-quit,
   the shutdown `finally` never runs and every spawned server is orphaned.
2. **No startup reconciliation.** On boot the sidecar does **not** reconcile the
   *persisted* runtime sessions against reality — so an orphan from a prior
   process lives forever. This is why the two servers we found were from the
   previous evening: some earlier sidecar exited non-gracefully, and no later
   boot ever reaped them.

The fix is well-supported because the persistence substrate **already exists**:
`RuntimeProcessManager` writes the child's process-group id to the session store
on spawn — `self.rstore.update_session(sid, pgid=pgid)` (`:639`, `:753`) — and
`RuntimeProfileStore.list_sessions()` (`runtime.py:346`) reads back
`RuntimeSession` records carrying `session_id`, `project_id`, `state`
(non-terminal = `"starting"`/`"running"`; `_TERMINAL = {"stopped","crashed"}` at
`:51`) and `pgid` (`runtime.py:122`). Nothing consumes that persisted pgid for
reaping. It's already on disk — we just never read it back to kill an orphan.

## Goal

No managed-local runtime process outlives the thing that owns it:

1. **Delete reaps first.** `delete_project` stops every runtime process for the
   project (in-memory *and* persisted) before `ws.destroy()`.
2. **Destroy is resilient.** A briefly-open worktree does not 500 the delete.
3. **Boot reaps orphans.** On sidecar startup, reconcile persisted non-terminal
   sessions against live process groups and kill/mark any that leaked across a
   non-graceful exit — closing the crash-exit hole `teardown_all` can't cover.

## Non-goals

- Not changing what the launch probe / delivery review *checks* (F152/F153/F154).
- Not redesigning the runtime profile/session schema — it already carries `pgid`
  and `state`; we only add a reap consumer.
- Not managing non-managed runtimes (an operator's own `npm run dev` in a real
  repo the CLI merely `import`ed) — reaping is strictly scoped to sessions this
  sidecar family spawned and persisted with a pgid.

## Design

### 1. A pgid-based reap that works from the persisted store, not just `_LIVE`

Add to `runtime_process.py` a helper that reaps by **persisted session**, so it
works even when `_LIVE` is empty (a fresh process that never spawned these
children):

```python
def reap_persisted_sessions(rstore: RuntimeProfileStore, *,
                            project_id: str | None = None) -> int:
    """Kill the process GROUP of every persisted session that is non-terminal,
    has a pgid, and whose group is still alive AND still ours. Marks each
    reaped session state="stopped", error="reaped_orphan". Scoped to one
    project when project_id is given, else all sessions in the store."""
    reaped = 0
    for sess in rstore.list_sessions():
        if project_id is not None and sess.project_id != project_id:
            continue
        if sess.state in _TERMINAL or sess.pgid is None:
            continue
        if not _pgid_is_ours(sess.pgid, sess):   # liveness + ownership guard
            rstore.update_session(sess.session_id, state="stopped",
                                  error="orphan_gone")   # already dead — just record it
            continue
        _kill_pgid(sess.pgid, grace=_GRACE_SECONDS)      # TERM then KILL the group
        rstore.update_session(sess.session_id, state="stopped", error="reaped_orphan")
        reaped += 1
    return reaped
```

- `_kill_pgid` = the group-kill already inside `_kill_group`/`_teardown_live`
  (`:1497`) factored to take a bare pgid (TERM → grace → KILL, `killpg`), so
  in-memory and persisted reaping share one implementation.
- `_pgid_is_ours` is the **safety guard against PID reuse**: a stored pgid could,
  after a reboot, belong to an unrelated process. Verify before killing — e.g.
  the leader pid's `cwd` is under this project's apply-workspace root (we already
  know the workspace path), or its argv matches the profile's `start`. If it
  can't be confirmed ours, treat as `orphan_gone` (record stopped, do **not**
  kill). Never `killpg` a group we haven't positively identified.

### 2. `delete_project` reaps before destroy (G1) + resilient destroy (G2)

`routes/coding.py:590-611`, inside the `with store.lock:` block, **before**
`ws.destroy()`:

```python
from errorta_council.coding import runtime_process as _runtime
_runtime.teardown_project(project_id)                 # in-memory servers (this sidecar)
_runtime.reap_persisted_sessions(store.runtime_store, project_id=project_id)  # cross-restart orphans
ws.destroy()
store.delete_project()
```

(Use whatever accessor already yields the project's `RuntimeProfileStore` — the
manager is constructed with `rstore=`; `RuntimeProfileStore.for_ledger(store)` is
the existing constructor used in tests. Reuse it, don't reconstruct ad hoc.)

Make destroy tolerant (G2) — `workspace.py` `destroy()` / the underlying
`rmtree`: pass an `onerror` handler that chmod-and-retries, and wrap the whole
removal in a bounded retry (e.g. 3 attempts, short backoff) so a file that
vanishes under us once doesn't fail the delete. After reaping in step 2 the tree
should be quiescent; the retry is belt-and-suspenders for a slow-dying child.

### 3. Boot-time orphan reap (G3, the root fix)

In the sidecar **startup** path (`server.py` lifespan, symmetric to the `:381`
shutdown `teardown_all`), before serving, reap orphans from prior lives across
**all** projects:

```python
# F157: reap managed-local runtime servers orphaned by a non-graceful prior exit
# (the shutdown teardown_all only runs on a clean exit; a crash/SIGKILL leaks them).
try:
    from errorta_council.coding import runtime_process as _runtime
    n = _runtime.reap_all_persisted_orphans()   # iterate every project's rstore
    if n:
        log.info("F157: reaped %d orphaned runtime server(s) from a prior sidecar", n)
except Exception:
    pass   # never block startup on reaping
```

`reap_all_persisted_orphans()` enumerates the per-project runtime stores (the
same discovery the project list uses) and calls `reap_persisted_sessions` for
each. This is the piece that would have prevented the observed leak: the next
sidecar boot after the crash would have killed the two `next dev` groups instead
of leaving them to rewrite `.next` for a day.

## Edge cases

- **PID reuse after reboot.** Central risk — handled by the `_pgid_is_ours`
  ownership guard in §1; an unconfirmed group is recorded `orphan_gone`, never
  killed. A stored pgid whose group is simply gone is a no-op state fixup.
- **Concurrent delete vs. a live run.** The existing `_thread_alive(project_id)`
  409 guard (`coding.py:599/606`) still fires first — we only reap *runtime
  preview* servers, which are orthogonal to the council run thread. Reaping runs
  under `store.lock`, same as today.
- **Graceful shutdown still works.** `teardown_all` at `server.py:381` is
  unchanged; boot reap is additive and idempotent (terminal sessions skipped).
- **Non-managed / imported-repo runtimes.** Only sessions persisted by *this*
  sidecar family carry a pgid in the store; an operator's own dev server was
  never recorded, so it's never a reap target.
- **A session mid-spawn (`state="starting"`, pgid not yet written).** `pgid is
  None` → skipped; the spawn either completes (pgid recorded) or the process is
  gone. No half-killed spawns.

## Testing

`python/tests/coding/` (extend `test_f146_slice_c_launch.py` / a new
`test_f157_orphan_reap.py`):

- `test_delete_reaps_running_server` — spawn a real long-lived server via the
  manager, delete the project, assert the process group is dead and the worktree
  is gone (the end-to-end repro of the live failure).
- `test_delete_survives_open_worktree` — a child writing into the tree during
  delete does not raise; delete returns `{"deleted": True}` (G2 regression).
- `test_reap_persisted_orphan_kills_group` — write a persisted non-terminal
  session whose pgid is a real live child (spawned in-test), call
  `reap_persisted_sessions`, assert the group is killed and the session is marked
  `state="stopped", error="reaped_orphan"`.
- `test_reap_skips_terminal_and_missing` — terminal sessions and dead-pgid
  sessions are not killed (dead-pgid → recorded `orphan_gone`).
- `test_reap_ownership_guard_spares_foreign_pgid` — a persisted pgid pointing at
  a process that is **not** ours (cwd/argv mismatch) is **not** killed (the
  PID-reuse guard). This is the safety-critical test.
- `test_boot_reap_clears_prior_orphans` — simulate a crash (populate the store
  with a live-child session, drop `_LIVE`), run the boot reap, assert the child
  is gone.

## Documentation

- `docs/CLI.md` runtime/delete sections: note that `delete` stops any running
  managed-local server for the project first, and that the sidecar reaps orphaned
  runtime servers on startup (so a crashed sidecar no longer leaves bound ports).
- `docs/coding/PM_REFERENCE.md`: mention `reaped_orphan` / `orphan_gone` session
  error markers if session states are enumerated there.

## Implementation notes (as built)

Deviations from the design above, chosen to reduce risk:

- **`_kill_pgid` is a new standalone function, not a refactor of `_kill_group`.**
  The proven Popen-driven in-memory teardown path is left untouched so the
  cross-restart reap can't regress it. `_kill_pgid` best-effort `waitpid(…,
  WNOHANG)`s the leader so a reaped process doesn't linger as a zombie (which
  `killpg(,0)` would still report as a live group); in production the orphan is
  init's child (`ECHILD`, harmless).
- **The ownership guard requires a positively-resolved cwd inside the workspace —
  no argv fallback.** A generic `npm run dev` cmdline is not a safe identity
  signal, so an unreadable cwd fails closed (the process is left alive rather than
  risk killing a stranger after PID reuse). `_pgid_is_ours` uses `psutil`
  (already a dependency).
- **The two unguarded `rmtree`s** (`ApplyWorkspace.destroy`,
  `LedgerStore.delete_project`) both route through a shared
  `resilient_rmtree` (chmod-and-retry `onerror` + bounded retry).
- **Boot reap** lives in the lifespan next to the existing coding boot-recovery
  block and is fully best-effort (never blocks `yield`).

## Out of scope

- A periodic (not just boot) orphan sweep — boot + delete + graceful-shutdown
  cover the observed failure; a watchdog sweep can follow if needed.
- Surfacing reaped orphans as an attention Problem — an info log is enough for
  the truthful-cleanup goal.
- The delete-path 500 → structured-error mapping in the CLI is subsumed: once
  delete no longer races a live writer it stops 500'ing; a genuine destroy error
  should still return a typed message rather than a bare 500 (nice-to-have).
