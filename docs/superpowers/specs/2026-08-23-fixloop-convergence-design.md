# Fix-loop convergence slice — design

**Date:** 2026-08-23
**Status:** approved for planning (autonomous session; decisions recorded below)
**Scope:** `errorta_liverun`, `errorta_model_gateway/providers/async_claude_cli.py`, `errorta_tools/runner/preview.py`, `pyproject.toml`, docs.

## Why

Live 2026-08-22/23 the supervisor (Slice 1) and fix loop (Slice 2) were mechanically complete but never *converged*: on the real defect of the day the sonnet dev exhausted its repository-read budget on a 657-file repo and misread the symptom. Four concrete causes were identified, in priority order, in `docs/liverun/README.md` "Status" and the 2026-08-23 run `916bbd`:

1. The dev seat on an `existing`-target project is `claude_cli.sonnet` with a 48-turn read budget and a 600 s turn timeout.
2. Triage keys on probe **ids** (`stall:journal-seq` …), so a profile that names a probe differently gets "no evidence class matched".
3. A project whose worktree was never seeded pauses the cycle at `fix_run_failed` with the reason discarded.
4. `window_shot` never captured RuneLite — diagnosed 2026-08-23 as **not** the `pgrep` pattern (it matches) but a missing `Quartz` module in the sidecar venv.

There is a real, reproducible stall to measure against: senditai-ng's tutorial preflight reads a `StateManager` nobody pumps between the feed gate and the `AgentLoop` (memory note `brain-preflight-never-pumps-state`). The slice is done when the loop is pointed at that stall and its outcome is recorded honestly — fixed, or paused with a reason a human can act on.

## Non-goals

- No change to the merge gate, guarded paths, caps, or the human-only verbs.
- No reviewer/tester model changes (YAGNI until the dev seat is shown to be the limit).
- No trusted unsandboxed gate (next slice).
- No senditai-ng edits in this slice: the brain bug is the measurement target.

## 1. Dev capacity for existing-repo fix cycles

### 1a. Dev route — profile-declared, applied through the existing control action

New optional profile key `fix_loop.dev_route` (string, default `claude_cli.opus`, validated `^[a-z_]+\.[a-z0-9][a-z0-9_.-]*$`). In `FixCycle._do_triage`, after the `target == "existing"` check and before the workspace is opened, the cycle reads the project's `run_config` members; if any `coding_role == "dev"` member's `gateway_route_id` differs from `dev_route`, it calls `errorta_council.coding.control_actions.assign_models_by_role(store, {"dev": dev_route}, ...)` — the same undoable, `pm_changes`-recorded path the Slack verb `reconfigure_team` uses — and emits `fix_team_model {project_id, role: "dev", from: [...], to: dev_route}`. The route is validated against `pm_reference.list_available_routes()` exactly as `reconfigure_team` does; an unavailable route pauses the cycle `fix_run_failed` with `detail="dev_route_unavailable:<route>"` rather than running a fix on the wrong model.

The change is deliberately persistent: the operator declared it in the profile, the control action records it as an undoable PM change, and a seat that can read a real repository is the right seat for every later fix cycle on that project too. Seam: `FixDeps.assign_dev_route(project_id, route) -> list[str]` (returns the prior routes), default wired to the control action.

### 1b. Repository-read budget and turn timeout

`async_claude_cli.py`:
- `_repo_read_max_turns()` default 48 → **80**; clamp stays `[2, 200]`; `ERRORTA_REPO_READ_MAX_TURNS` still overrides.
- New `_repo_read_timeout_s()` — `ERRORTA_REPO_READ_TIMEOUT_S`, default **1500**, clamp `[600, 3000]`. The retrieval attempt (the one with `cwd_override = repo_read_root`) runs with `timeout_seconds=max(request.timeout_seconds, _REPO_READ_TIMEOUT_S)`; the plain fallback keeps `request.timeout_seconds` unchanged. The read-only turn is where the budget is spent; the fallback has no repository to read.

Both values are snapshotted at import (existing behaviour); the docstring states that the env must be set on the invocation that spawns the sidecar.

### 1c. Fix-loop idle budget follows the turn timeout

`profile.py`: `FIX_CAP_DEFAULTS["idle_timeout_s"]` 1200 → **2400**; `MIN_IDLE_TIMEOUT_S` 600 → **1500** with the comment pointing at `ERRORTA_REPO_READ_TIMEOUT_S`'s default (a single repo-read turn must be able to finish before the cycle calls the run idle). `FixLoop.idle_timeout_s` default 2400. `fixloop.DEFAULT_IDLE_TIMEOUT_S` 1200 → 2400. The operator profile `~/.errorta/liverun/profiles/osrs.yaml` is updated to `idle_timeout_s: 2400` (it is outside the repo; noted in the README). Caps remain lower-only.

## 2. Triage by probe kind

`brief.EvidenceBundle` gains `stalled_probe_kind: str | None = None`. `Supervisor._fix_bundle` resolves it from `self.profile.watch` by the id it already extracts (`w.id == probe_id → w.probe.kind`); an id with no matching probe leaves it `None`.

`triage._SIGNATURES` classify by kind when present, id otherwise (back-compat for bundles built without a profile, e.g. existing tests):

| class | kind | legacy id (fallback only) |
|---|---|---|
| `brain_pid_dead` | `remote_pid_alive` | `stall:brain-alive` |
| `brain_log_stall` | `remote_file_mtime_advancing` | `stall:brain-log` |
| `journal_stall` | `remote_stdout_advancing`, `remote_stdout_matches` | `stall:journal-seq`, `stall:feed-live` |
| `client_port_dead` | `http` (and not `brain_pid_dead`) | `stall:client-state` |

`elapsed_lt_s` maps to no class (a session-clock stall is not a defect). A single helper `_stall_kind(bundle) -> str | None` returns the kind if set, else the kind implied by the legacy id table, else `None`; every signature reads that. The README's "name probes exactly so" items (8 in "what the first live runs settled", and the follow-up bullet) are rewritten.

## 3. The fix cycle seeds a missing worktree

New seam `FixDeps.seed_workspace(project_id) -> bool` (True when it created one). Default: `CodingWorkspace(project_id, store)`; if `not ws.exists()`, `ws.setup(target=proj.target, repo_path=proj.repo_path)` and return True; else return False (no re-stamp of `seed_head` on an existing workspace). `_do_triage` calls it immediately before `deps.workspace(project_id)`; on True it emits `fix_workspace_seeded {project_id, repo_path}`. A seeding failure pauses `fix_run_failed` with `detail="seed:<ExceptionType>"`.

The existing `except Exception` around `deps.workspace` keeps its shape but records `detail=f"{type(exc).__name__}:{str(exc)[:80]}"` so a 409 "no worktree" (or anything else) is no longer reduced to `HTTPException`.

## 4. `window_shot` — the honest diagnostic and the dependency

- `pyproject.toml` dependencies: `"pyobjc-framework-Quartz>=10; sys_platform=='darwin'"`.
- `errorta_tools/runner/preview.py`: `quartz_available() -> bool` (import probe, cached). `_run_window_shot` reports `detail="quartz_unavailable"` when `sys.platform == "darwin"` and Quartz cannot be imported, `"no process matched"` when `pgrep` found nothing, and `"no window captured"` only when a window genuinely could not be resolved or captured. Three different failures stop reading as one.
- README follow-up bullet corrected: the cause was the dependency, not the pattern.

## Data flow (one fix cycle, after this slice)

stall → evidence → teardown → `_fix_bundle` (+kind) → `triage.classify` (by kind) → `_do_triage`: target check → `assign_dev_route` (maybe) → `seed_workspace` (maybe) → open workspace → brief → Focus + task → dev run (opus, 80 read turns, 1500 s read turn) → watch (idle 2400 s) → gate → staged accept → deploy → relaunch.

## Error handling

Every new step pauses `fix_run_failed` with a specific `detail`; nothing new retries, and nothing new touches the accept/merge path. Events are posted through the existing `_event` channel so Slack narration needs no new kinds beyond `fix_team_model` and `fix_workspace_seeded` (both routine, not mandatory-when-muted).

## Testing

- `tests/liverun/test_triage.py`: kind-based classification for each row above; `elapsed_lt_s` → no class; legacy-id fallback when `stalled_probe_kind is None`; a renamed probe (`id: j`) with kind `remote_stdout_advancing` classifies `journal_stall`.
- `tests/liverun/test_supervisor.py`: `_fix_bundle` carries the kind; unknown id → `None`.
- `tests/liverun/test_fixloop.py`: `assign_dev_route` called only when a dev seat differs; event shape; unavailable route pauses; `seed_workspace` True/False paths and event; workspace failure detail carries the message.
- `tests/liverun/test_profile.py`: `dev_route` validation (default, bad string, unknown key still rejected); `idle_timeout_s` floor 1500 and default 2400.
- `tests/test_async_claude_cli.py`: default 80; `ERRORTA_REPO_READ_TIMEOUT_S` clamp; the retrieval attempt gets the raised timeout and the plain attempt does not.
- `tests/` for preview/steps: `quartz_unavailable` vs `no process matched` vs `no window captured`.
- Live measurement (not a test): resume `osrs`, relaunch, let the cycle run against the preflight bug; record run id, triage result, dev outcome, and whether the delivered diff touches the preflight poll loop. The README "Status" section is updated with the result either way.

## Decisions taken in this session (flag if you disagree)

- Persisting the dev route change on the project (1a) instead of a per-run override: the run-start path re-persists members anyway, and the control action is undoable.
- Leaving senditai-ng unfixed until the loop has had its attempt.
- Profile edits outside the repo: `journal-seq stall_after_s` 240 → 2100 (done), `idle_timeout_s` 1200 → 2400 (this slice).
