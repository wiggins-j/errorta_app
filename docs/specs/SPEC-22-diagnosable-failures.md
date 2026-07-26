# Spec 22 — Diagnosable failures

**Source:** `ROADMAP-autonomy.md` Phase 1, gap **G3** — "when this system fails,
it fails silently." Concretely: an `errorta new` `500` that has now survived
three debugging sessions unexplained, and the on-disk residue every failed
attempt left behind.
**Target version:** v0.1 (CLI + sidecar + ledger)
**Status:** proposed
**Owner:** wiggins-j

---

## Problem

Four independent defects, one shape: **the system destroys the evidence of its
own failure.**

**1. The CLI throws the sidecar's stdio away.** `errorta_cli/sidecar.py:122-130`:

```python
def _launch(argv: list[str], env: dict[str, str]) -> subprocess.Popen:
    """Spawn the sidecar process (its own session so it outlives this call)."""
    return subprocess.Popen(  # noqa: S603 — fixed argv, no shell
        argv,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
```

Every line the sidecar ever writes — uvicorn's startup banner, every
`logging.getLogger(...).warning` in `server.py`'s lifespan, and **every unhandled
route traceback** — goes to `/dev/null`. Not "hard to find": *destroyed at the
syscall*. When a CLI-spawned sidecar dies during startup, the only artifact is
`sidecar.py:437-439`:

```
the sidecar exited during startup (code {proc.returncode})
```

An exit code. For a Python process that just printed a full traceback we
deliberately discarded.

**2. A `500` carries no correlation.** `errorta_cli/client.py:246` is the
terminal branch of `_raise_for_status` — everything not specifically classified
lands here:

```python
raise CliError(f"sidecar returned {status}: {message or detail!r}", code=code)
```

For an unhandled route exception FastAPI/Starlette returns the literal body
`Internal Server Error`, so the operator sees `sidecar returned 500: 'Internal
Server Error'` — a string with **zero** greppable content. There is no
`@app.exception_handler` and no error middleware anywhere in `errorta_app/`
(the only `add_middleware` call is CORS, `server.py:466`). The traceback exists,
briefly, inside the sidecar's in-memory ring buffer; nothing on the CLI side can
reach it and nothing writes it down.

**3. A failed create leaves a half-made project.** `create_project`
(`routes/coding.py:549-580`) validates carefully and then writes **before** its
last fallible step:

```
:570  store.create_project(...)          # writes <dir>/project.json
:576  grounding_result = _apply_grounding_payload(store, body.grounding)
:577  out = {"project": _project_out(store)}
```

`_apply_grounding_payload` (`:396`) imports `errorta_project_grounding`,
validates roots, and can raise (`HTTPException` at `:418`, or anything the
bootstrap raises) — *after* `project.json` is on disk. `_project_out` can too.
There is no `try/except` and no rollback. `LedgerStore.__init__`
(`ledger.py:548-564`) doesn't create the directory, but `_atomic_write_json` does
(`ledger.py:67`), and the first `store.lock` acquisition drops a `.run.lock`
next to it (`coding/locks.py:69`). So a failed create leaves a directory that a
retry then trips over.

**4. `delete` leaves residue — and can strand a project permanently.**
`delete_project` (`routes/coding.py:591-625`) removes exactly two things:
`ws.destroy()` (`:622`) and `store.delete_project()` (`:623`). Both go through
`resilient_rmtree` (`errorta_tools/runner/apply_workspace.py:104-127`), which
**raises on its final attempt** — so a delete that fails part-way through the
rmtree returns a `500` *and* leaves a partially-removed tree. Because both
`GET` and `DELETE /coding/projects/{id}` begin with `store.get_project()` and
404 when `project.json` is absent (`:585-588`, `:596-599`), a tree that lost its
`project.json` mid-rmtree is **unreachable through the API forever**.

This is observable on the live store right now:

| `~/.errorta/council/coding-projects/<id>` | contents | API reachable? |
|---|---|---|
| `pocketboard2` | `run_config.json`, `run_state.json` — **no `project.json`** | no (404 on GET *and* DELETE) |
| `punprod` | `decisions.jsonl`, `signals.jsonl` — **no `project.json`** | no |

And the CLI adds its own: `errorta delete` (`errorta_cli/commands/project.py:217-231`)
issues the `DELETE` and switches the session pointer, but never touches
`${ERRORTA_HOME}/cli-team-drafts/<id>.json`. `teamdraft.clear` (`teamdraft.py:93`)
has exactly **one** caller in the whole tree — `commands/team.py:201`, the
`team create` reset path. Delete is not it.

The accumulated effect, again on the live store: `~/.errorta/council/apply-workspaces`
holds **144 entries** covering **39 project ids that no longer exist**
(`coding-p`, `coding-p2`, `coding-r1` … each with its `.source.json` and
sometimes a `.worktrees.json`). Nothing reaps them.

### Why this is the first spec in the roadmap

Because it is the cheapest one, and it is the only one whose absence makes the
other six unverifiable. SPEC-23 turns a heuristic stop into a negotiated
last-word turn — if that turn misbehaves, today we would debug it exactly the
way we debugged the `500`: by guessing. Every item below is a small, local,
well-understood change: one `Popen` kwarg plus a handler, one exception handler,
one `try/except` around an already-written file, one delete sweep. No engine
change, no new subsystem, no schema change. The roadmap's own success criterion
#3 — *"any `500` or turn rejection leaves a traceback and a correlation id"* —
is entirely satisfied by Items 1 and 2.

## Why the existing machinery didn't catch it

The repo has **four** logging mechanisms. Each is defeated by a different thing.

| Mechanism | Where | Why it didn't help |
|---|---|---|
| `settings.json`'s `"log_level": "info"` | applied at `server.py:203` via `settings.py:321-334` | It only calls `logger.setLevel(...)` on root + the three uvicorn loggers. It configures **verbosity, not destination.** With stdio at `DEVNULL` it is a volume knob on a disconnected speaker. |
| In-memory ring buffer | `LogBuffer`, installed `server.py:158-173` on root/`uvicorn`/`uvicorn.error`/`uvicorn.access` | 5 MB default (`log_buffer.py:14`), **process-lifetime only**. It genuinely captures the unhandled-route traceback (uvicorn logs it to `uvicorn.error`) — and loses it the moment the sidecar exits. |
| `/diagnostics/log-tail` | `routes/diagnostics.py:117-131`, redacted via `_redact_line` (`:25`) | The correct read path for the buffer above — and **no CLI command calls it.** `grep -rn "log-tail" errorta_cli/commands/` returns nothing. You cannot reach it from the tool that showed you the error. |
| `ERRORTA_LOG_FILE` | `server.py:177-198` | A real on-disk sink — **off by default, env-var-only**, and by its own comment writes "every log line … UNREDACTED". Nobody sets it, and it is not safe to default on (see Item 1's redaction requirement). |

Two more contributors:

- **`${ERRORTA_HOME}/logs/` exists and is empty.** Its only referent in the
  entire repo is the Tauri "reveal logs folder" command
  (`src-tauri/src/shell_cmds_impl.rs:71-72`, `:119-127`). There is a **button in
  the desktop app that opens a directory nothing has ever written to.**
- **The desktop app is strictly better instrumented than the CLI.** Tauri drains
  the sidecar's pipes (`src-tauri/src/sidecar.rs:528-541`, forwarding
  `CommandEvent::Stdout`/`Stderr` to the Rust shell's stderr;
  `docs/SIDECAR_LIFECYCLE.md:87` documents this as the v0.1 behaviour). The CLI
  path — the one every autonomous run uses — is the one with no drain at all.
  The asymmetry is why "run it from the app and see" was never a workaround.

Finally, there is no anti-drift canary here of the kind Spec 19 Item 1
introduced, because there is nothing to compare: the failure mode is *absence*,
and absence has no second declaration to disagree with.

## Goals

- A CLI-spawned sidecar's stdout and stderr **land on disk**, bounded, and
  survive the process that produced them.
- Every unhandled route exception produces a **correlation id** that appears
  both in the operator-facing error and in the persisted traceback, so
  `grep <id>` is a complete debugging first step.
- A failed `POST /coding/projects` leaves **no** project directory — a retry
  with the same id behaves like a first attempt.
- `delete` removes **all** per-project state this repo owns, on both sides of
  the HTTP boundary, and a partially-failed delete leaves something the API can
  still act on.
- Secrets never reach the new log.

## Non-goals

- **Not a telemetry or observability platform.** No metrics, no exporters, no
  remote sink, no OpenTelemetry. Files under `${ERRORTA_HOME}`, read with
  `tail` and `grep`.
- **Not structured tracing across turns.** A per-turn trace id threaded through
  the council loop is a real idea and belongs with SPEC-24's governance
  visibility. This spec's correlation id is scoped to **one HTTP request**.
- **Not a change to the sidecar lifecycle model.** Spawn/adopt/refuse
  (`sidecar.py:450-563`), the co-drive policy, the run lock, and the watchdog
  registry are untouched. Item 1 changes two `Popen` kwargs, not who owns the
  process.
- **Not a redesign of the ledger's on-disk format.** Item 3 wraps the existing
  write sequence; it does not introduce a journal, a WAL, or a staging dir.
- **Not retroactive cleanup of the 39 orphaned apply-workspaces** on the
  maintainer's live store. Item 4 stops the bleeding; a one-off sweep is a
  follow-up (and several of those ids are plainly test fixtures that escaped
  `--home` isolation — a separate bug).
- **Not re-specifying Spec 19.** See Item 5.

---

## Item 1 — Sidecar stdio to a bounded, rotating, redacted log

**Design.** `_launch` (`errorta_cli/sidecar.py:122-130`) stops passing
`DEVNULL`. It opens `${ERRORTA_HOME}/logs/sidecar.log` in append mode and passes
that fd as both `stdout` and `stderr` (merged — interleaving is the point; a
traceback split across two files is worse than either). `start_new_session=True`
is unchanged; the fd is inherited by the detached child and outlives the CLI
process that opened it.

**Rotation is not optional.** A long autonomous run at `log_level: info` writes
continuously, and the child holds the fd for its whole life, so nothing can
rotate it *underneath* the running sidecar. Two halves:

- **At spawn (CLI side, `_launch`)** — before opening, if `sidecar.log` exceeds
  `_LOG_ROTATE_BYTES` (propose **8 MB**), rename it to `sidecar.log.1`
  (displacing any previous `.1`), and open fresh. Keep exactly one generation:
  two files, hard-capped at 16 MB total, forever. This is a plain `os.replace`
  under the existing `_home_lock` (`sidecar.py:260-288`), which already
  serialises the discover-or-spawn decision — no new lock.
- **In-process (sidecar side)** — the sidecar caps its *own* growth so a
  single long-lived process cannot exceed the budget between spawns. The
  cheapest honest option is a size check in the same lifespan block that already
  handles `ERRORTA_LOG_FILE` (`server.py:177-198`): when the sidecar's own
  writes cross the cap it logs one `WARNING` and stops writing the sink. **A
  truncating in-process rotation is explicitly rejected** — the CLI's fd points
  at an inode, so truncation would produce a sparse file and a `.1` rename would
  silently orphan the live fd.

**Redaction.** `ERRORTA_LOG_FILE`'s own comment concedes it writes
**UNREDACTED**, which is why it cannot simply be turned on by default. The new
sink must run the same pipeline `/diagnostics/log-tail` already applies —
`_redact_line` (`routes/diagnostics.py:25`) over
`errorta_diagnostics/redact.py`'s `redact_tokens` (`:97`), which matches
`sk-…` / `sk-ant-…` shapes (`redact.py:23-24`).

Two things must be checked before this ships, because a plain fd redirect
bypasses `logging` entirely and therefore bypasses any formatter-level
redaction:

- **What the sidecar prints outside `logging`.** `grep -n "print(" errorta_app/server.py`
  is empty today, which is good — but uvicorn's own startup output and any
  third-party library's `print` go straight down the fd. The mitigation is that
  redaction must be enforced by a **`logging` handler on the sidecar side
  writing to the file directly**, not by hoping the fd only carries redacted
  bytes. I.e. the fd redirect catches crashes and non-`logging` output; a
  redacting `FileHandler` on root/`uvicorn.*` carries the structured lines.
- **Provider keys.** `~/.errorta/provider-keys.json` is 0600. Nothing in
  `server.py`'s lifespan logs its contents today, and `ERRORTA_SIDECAR_TOKEN`
  (`sidecar.py:65`) is passed via env, not argv, so it does not appear in a
  process listing or a spawn log line. **This must be re-verified as an
  acceptance test, not assumed** — the whole point of the item is that we stop
  relying on "nobody logs secrets" being true by inspection.

**Acceptance.** After a CLI-spawned sidecar starts, `${ERRORTA_HOME}/logs/sidecar.log`
is non-empty and contains uvicorn's startup lines. A sidecar killed with an
uncaught exception leaves its traceback in that file. `sidecar.log` never
exceeds the cap; after crossing it, `sidecar.log.1` exists and total bytes stay
bounded across repeated spawns. A line containing an `sk-ant-…`-shaped token is
written to the file redacted. `errorta status` prints the log path.

## Item 2 — A correlation id on every unhandled route exception

**Design.** Add one `@app.exception_handler(Exception)` in
`errorta_app/server.py` (there are none today). On an unhandled exception it:

1. mints `err_id = f"e-{uuid.uuid4().hex[:12]}"` — the same shape
   `LedgerStore.record_decision` already uses for `decision_id`
   (`ledger.py:929`), so ids read consistently across the store;
2. logs the full traceback via `logging.getLogger("errorta.error").exception(...)`
   with the id in the message — which reaches the ring buffer, the Item 1 file
   sink, and `ERRORTA_LOG_FILE` if set;
3. returns `500` with `{"detail": {"code": "internal_error", "error_id": err_id,
   "hint": "grep <err_id> in ${ERRORTA_HOME}/logs/sidecar.log"}}` instead of the
   bare `Internal Server Error` string.

The dict-shaped `detail` is deliberate: `client.py`'s `_classify` already pulls
`(code, message)` out of a dict `detail` (`client.py:249`), so the CLI's
existing classification machinery reads it with no change to the parser. Only
the final `raise CliError` at `client.py:246` is extended to append
`(error id: e-…)` when the payload carries one.

**Ledger decisions are the wrong place for this.** The roadmap suggests "the
sidecar log and/or a ledger decision"; `record_decision` requires a
`LedgerStore`, and the majority of 500-able routes either have no project
context or are failing *because* the store is broken. A decision record is
therefore a **follow-up for the run loop specifically** (SPEC-23's last-word
turn is the natural consumer), not the mechanism here. The log is the sink that
always exists.

**Acceptance.** A route that raises `ZeroDivisionError` returns a `500` whose
body carries a `e-…` id; the same id appears in the sidecar log immediately
above the traceback; the CLI's rendered error contains the id. Handlers that
raise `HTTPException` deliberately (422/404/409 — hundreds of call sites in
`routes/coding.py`) are **unaffected**: Starlette dispatches those to its own
handler and never reaches this one.

## Item 3 — `POST /coding/projects` leaves nothing behind on failure

**Design.** Wrap everything from `store.create_project(...)`
(`routes/coding.py:570`) to the end of the handler in a `try/except
BaseException`. On failure, before re-raising: remove `<ledger_root>/<id>/`
**if and only if** this request created it. The guard matters — a `409`-shaped
race where a concurrent create won must not delete the winner's project. The
cheap, correct check is to test `self.dir.exists()` *before* `create_project`
and only clean up when it did not.

Cleanup uses `resilient_rmtree` (`apply_workspace.py:104`) for the same reason
delete does, and must itself be exception-swallowing: a cleanup failure must not
mask the original error. It logs at `WARNING` with the Item 2 correlation id so
the residue is at least *named* when it cannot be removed.

The `.run.lock` file (`coding/locks.py:69`) lives inside the project directory,
so the directory rmtree covers it; no separate handling.

**Δ note — why not "validate everything first".** `create_project` already does
this as far as it can: `_validate_repo_path`, `_validate_delivery_root`, and
`_validate_grounding_payload` all run before the write, with the comment at
`:563-564` explicitly stating the intent ("so a bad grounding payload can't
leave a half-created project behind"). That intent is correct and was correctly
implemented for *validation* failures. It cannot cover *execution* failures —
`_apply_grounding_payload` (`:396`) does real work (imports, bootstrap jobs,
filesystem writes) that no amount of upfront validation makes infallible.
Compensating cleanup is the only closure.

**Acceptance.** With `_apply_grounding_payload` monkeypatched to raise, `POST
/coding/projects` returns non-2xx and `<ledger_root>/<id>/` does not exist. An
immediately-repeated create with the same id succeeds. A create that fails
against a **pre-existing** project directory does not delete it.

## Item 4 — `delete` removes every piece of per-project state

**Design.** Two halves, matching where the state lives.

**Server side** (`routes/coding.py:591-625`). Today `ws.destroy()` + 
`store.delete_project()`. The enumeration of what else is keyed by project id, 
verified against the live store:

| Location | Keyed by | Removed today? |
|---|---|---|
| `council/coding-projects/<id>/` | project id | yes — `store.delete_project()` (`ledger.py:666-684`) |
| `council/apply-workspaces/coding-<id>` + `.source.json` | project id | yes — `ApplyWorkspace.destroy` (`apply_workspace.py:467-476`) |
| `council/apply-workspaces/coding-<id>.worktrees` + `.worktrees.json` | project id | yes — `_clear_owned_worktrees` (`:453-465`) |
| `council/rooms/` | room id, not project id | n/a — no project-keyed entry |
| `council/runner-artifacts/`, `council/runner-runtime/` | run / profile id | n/a — reaped by `runtime_process.teardown_project` + `reap_persisted_sessions` (`:617-619`), best-effort |
| `council/context-manifests/cm-*.json` | manifest id | **no** — not project-keyed, cannot be swept by id |
| `council/wizard-sessions/wiz-*.json` | wizard id | **no** — not project-keyed |
| `council/runs/*.jsonl` | run id | **no** — not project-keyed |
| `cli-team-drafts/<id>.json` | **project id** | **no** — see below |

So the server-side gap is narrower than it looks: the three project-id-keyed
locations are already handled. What is missing is **failure behaviour**, not
coverage. The change is:

- Delete `project.json` **first**, then rmtree the rest. If the rmtree
  partially fails the directory is already un-listable and the route can be made
  idempotent; today the opposite order strands the tree (the `pocketboard2` /
  `punprod` shape).
- Make `DELETE /coding/projects/{id}` **idempotent for cleanup**: when
  `project.json` is absent but the directory exists, do not `404` — sweep the
  residue and return `{"deleted": true}`. This is the only change that makes the
  two already-stranded directories reachable, and it is the correct semantic for
  a delete regardless.

**CLI side** (`errorta_cli/commands/project.py:217-231`). After a successful
`DELETE`, call `teamdraft.clear(ctx.home, project_id)` (`teamdraft.py:93`) —
today its only caller is `commands/team.py:201`. Do this **after** the HTTP call
succeeds, so a `409` ("project run is still active") does not destroy a draft
the operator still needs.

The `.errorta-project` pointer file written by `_write_binding`
(`commands/project.py:67-79`) lives in a **user** directory, not under
`${ERRORTA_HOME}`. It is deliberately **not** removed: the delivery directory is
the user's, and deleting files inside it on a project delete is a larger
blast radius than this spec should take. `ctx.switch_project(None)` (`:230`)
already unbinds the session.

**Acceptance.** After `errorta delete <id>`: no `coding-projects/<id>`, no
`apply-workspaces/coding-<id>*`, no `cli-team-drafts/<id>.json`. A second
`errorta delete <id>` reports not-found cleanly rather than erroring on residue.
A `DELETE` against a directory with no `project.json` sweeps it and succeeds.
A `DELETE` refused with `409` leaves the team draft intact.

## Item 5 — A shipped binary must be able to say what it is

**Design.** No new mechanism. This item exists to name the principle and bind it
to this spec's goal, because the specific instance is already fixed:
`SPEC-19-version-identity-and-build-provenance.md` covers the canonical version
(Items 1–3) and the `_build_info.json` commit stamp (Item 4).

The generalisation SPEC-22 asserts: **an identity signal that can be wrong is a
diagnostic liability, not a diagnostic.** The alpha.11 recut's stale version
string did not merely mislead — it actively *routed* a debugging session down a
wrong path (SPEC-19 §Problem), which is strictly worse than having printed
nothing. The same standard applies to everything this spec adds: an error id
that does not appear in the log, or a log path printed by `errorta status` that
nothing writes to, would be the same defect in a new costume. That is exactly
what `${ERRORTA_HOME}/logs/` is today — a directory the desktop app offers to
open (`shell_cmds_impl.rs:71-72`) and nothing populates.

**Acceptance.** Nothing to implement. Two cross-cutting checks the other items
must satisfy: every id the CLI prints resolves to a line in the log
(Item 2's test), and every path the CLI prints exists and is written
(Item 1's test). Ship after SPEC-19 lands so `errorta status`' version line is
trustworthy when it starts printing a log path next to it.

---

## Implementation notes

- **`python/errorta_cli/sidecar.py`** — `_launch` (`:122-130`): fd redirect +
  pre-spawn rotation; a new `_log_path(home)` helper next to
  `config.sidecar_record_path` (`config.py:82`). `_launch`'s signature gains
  `home` (it is a documented monkeypatch seam per the module docstring, so the
  CLI test suite's fakes — `tests/cli/` — must be updated in the same change).
- **`python/errorta_app/server.py`** — the `Exception` handler (Item 2) next to
  the existing `app.add_middleware` block (`:466`); the redacting file handler
  in `lifespan` alongside the `ERRORTA_LOG_FILE` block (`:177-198`), sharing its
  best-effort "an unwritable path must not block startup" discipline.
- **`python/errorta_app/routes/coding.py`** — `create_project` (`:549-580`)
  compensating cleanup; `delete_project` (`:591-625`) ordering + idempotence.
- **`python/errorta_cli/client.py`** — `:246` only: append the error id when the
  payload carries one. `_classify` is unchanged.
- **`python/errorta_cli/commands/project.py`** — `_delete_call` (`:217-231`):
  one `teamdraft.clear` call.
- **`python/errorta_app/paths.py`** — add `logs_dir()` next to the existing
  accessors (`:113-221`); it is the first Python referent for a directory only
  Rust has named so far (`shell_cmds_impl.rs:119-127`, which honours
  `ERRORTA_LOGS_DIR` — the Python side must resolve the same way or the app's
  "open logs folder" button points somewhere else).
- **No engine change.** Nothing in `errorta_council/coding/` is touched except
  the `delete_project` call sequence it is driven through.

## Edge cases

- **An adopted sidecar.** `resolve` adopts an already-running sidecar
  (`sidecar.py:498-515`) without calling `_launch`. Its stdio is whoever spawned
  it — the desktop app's Rust drain, or an earlier CLI's fd. Item 1 changes
  nothing for adoption, and must not: re-pointing a live process's fds is not
  possible. The in-process redacting handler (same item) is what covers the
  adopted case, which is why both halves are needed.
- **`${ERRORTA_HOME}/logs` unwritable / read-only home.** Fail open: fall back
  to `DEVNULL` and warn once on stderr, exactly as `_home_lock` degrades on a
  platform without `fcntl` (`sidecar.py:268-278`). A CLI that refuses to run
  because it cannot open a log file is a worse product than one that logs
  nothing.
- **Two CLI processes spawning concurrently.** Both would rotate. The pre-spawn
  rotation runs inside `_home_lock`, which already serialises spawn
  (`sidecar.py:464`), so only one wins and the other opens the fresh file.
- **`--home` isolation.** The log is under `${ERRORTA_HOME}`, so a test or an
  alternate home gets its own — matching `cli-team-drafts` (`teamdraft.py:45`)
  and `sidecar.json`.
- **The desktop app's sidecar.** It is spawned by Tauri, not `_launch`, and its
  stdio already goes to the Rust shell's stderr (`sidecar.rs:528-541`). Item 1
  must not double-write; the in-process handler writing to `logs/sidecar.log` is
  shared, the fd redirect is CLI-only.
- **A `500` from a route that is *already* returning a dict detail.** Item 2
  only fires for **unhandled** exceptions; deliberate `HTTPException`s keep
  their existing shape and codes, which `client.py:206-246` classifies today.
- **Item 3 cleanup racing a concurrent create of the same id.** Guarded by the
  "directory did not exist before this request" precondition; when it did exist,
  cleanup is skipped and only the error propagates.
- **Item 4's idempotent delete and a live run.** The `_thread_alive` `409` check
  (`:600`, re-checked under the lock at `:607`) must run **before** the residue
  sweep, or a sweep could delete under a running run.

## Testing

Targeted, all runnable without a real sidecar except where noted.

- **Item 1 — stdio reaches disk.** With a fake `_serve_argv` that prints a
  known marker to stderr and exits non-zero, `spawn()` raises
  `SidecarUnreachable` **and** `${ERRORTA_HOME}/logs/sidecar.log` contains the
  marker. This is the exact regression: today the marker is unrecoverable.
- **Item 1 — rotation.** Pre-seed `sidecar.log` past the cap, spawn, assert
  `sidecar.log.1` exists, `sidecar.log` is small, and total bytes across N
  spawns stay under 2× the cap (the unbounded-growth lock).
- **Item 1 — secrets never reach the log.** Log a line containing
  `sk-ant-` + 24 chars through the sidecar's root logger; assert the on-disk
  file contains the redaction marker and **not** the token. Plus a standing
  assertion that the spawn env's `ERRORTA_SIDECAR_TOKEN` value
  (`sidecar.py:65`, `:376-383`) never appears in the file.
- **Item 2 — traceback + id.** Mount a test route on the app that raises;
  assert the response body carries `error_id` matching `^e-[0-9a-f]{12}$`, and
  that the same id plus a `Traceback` appear in the captured log records.
- **Item 2 — deliberate HTTPExceptions unaffected.** A `404` from
  `GET /coding/projects/nope` still returns `{"detail": "project not found"}`
  and mints no id (the no-false-positives lock).
- **Item 2 — the CLI surfaces it.** `_raise_for_status` against a stub `500`
  carrying `error_id` raises a `CliError` whose message contains the id;
  against a `500` **without** one, the message is unchanged (back-compat with an
  older sidecar).
- **Item 3 — no residue.** Monkeypatch `_apply_grounding_payload` to raise;
  `POST /coding/projects` fails and `tmp_path/<id>` does not exist; a retry with
  the same id then succeeds. Separately: a pre-existing project directory is
  **not** removed when a create against it fails.
- **Item 4 — full cleanup.** Create a project, seed a team draft, delete;
  assert `coding-projects/<id>`, `apply-workspaces/coding-<id>*`, and
  `cli-team-drafts/<id>.json` are all gone.
- **Item 4 — the stranded-directory lock.** Construct a directory with
  `run_config.json` but no `project.json` (the observed `pocketboard2` shape);
  `DELETE` sweeps it and returns success rather than `404`.
- **Item 4 — a refused delete preserves the draft.** With `_thread_alive`
  patched true, the `409` leaves `cli-team-drafts/<id>.json` in place.

## Documentation

- **`docs/SIDECAR_LIFECYCLE.md`** — replace `:87-88` ("v0.1 logs sidecar
  stdout/stderr straight to the Rust shell's stderr. Structured logging + an
  in-app log viewer is a v0.5 problem") with the real behaviour: where the log
  is, how big it gets, that it is redacted, and that the app and CLI paths
  differ.
- **`docs/CLI.md`** — a short troubleshooting entry: a `500` prints an error id;
  `grep <id> ~/.errorta/logs/sidecar.log`. This is the sentence that would have
  ended the three-session investigation.
- **`docs/BUILD_AND_RELEASE.md`** — nothing; this spec adds no release step.

## Out of scope / follow-ups

- **A one-off reaper** for the 39 orphaned `apply-workspaces` entries and the
  two stranded project directories on the maintainer's live store. Item 4's
  idempotent delete makes the latter reachable; a sweep command
  (`errorta doctor --reap`) is separate work.
- **Why those 39 orphans exist at all.** Ids like `p2b`, `strict-rej`, `w4` are
  test-fixture-shaped, which suggests some test suite wrote into the real
  `${ERRORTA_HOME}` instead of an isolated `--home`. That is its own bug and
  worth finding; it is not this spec.
- **The root-cause of the original `500`.** This spec does not claim to fix it.
  It makes the *next* occurrence take one command instead of three sessions.
  Filing it as an open bug and closing it with the evidence Item 1 produces is
  the intended sequence.
- **A per-turn trace id** threaded through the council run loop, and recording
  failures as ledger decisions — the natural home is SPEC-23/24, where the PM
  needs to *read* the failure, not just an operator.
- **Exposing the log through `/diagnostics`** (a `log-file-tail` sibling to
  `log-tail` at `routes/diagnostics.py:117`) so the desktop app's empty logs
  folder becomes a real in-app viewer.
- **Turning `ERRORTA_LOG_FILE` on by default** once redaction is enforced at the
  handler — at which point it and Item 1's sink are the same mechanism and one
  of them should be retired.
