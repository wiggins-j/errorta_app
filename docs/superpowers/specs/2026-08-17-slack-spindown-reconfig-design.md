# Slack Spin-Down + Team Reconfigure — Design (Slice 3)

**Date:** 2026-08-17
**Status:** Design (autonomous continuation). Not yet implemented.
**Depends on:** studio manager (Slice 1) + run control (Slice 2), merged.
**Modules:** `errorta_slack/{store,provisioning,studio_tools,studio_concierge,tools,concierge}.py`.

---

## 1. Problem

The vision is a PM that spins projects **up and down** and **sets up teams**. Slice 1
spins up + sets the initial team; Slice 2 runs them. Missing: **spin a project down**
and **change a project's team mid-flight**. This slice adds both.

## 2. Grounded facts (from the spin-down/reconfig mechanics investigation)

- **No first-class "archived" project status.** `Project.status` ∈ `active|paused|done|failed`
  (unvalidated string). Hard-delete exists (`routes/coding.py:713` — worktree destroy +
  runtime reap + ledger delete) but **refuses (409) while a run is live** and is
  **irreversible**. → **Default spin-down is SOFT and reversible: pause + archive the
  Slack channel + unbind.** Hard delete is deferred (needs a cancel→drain→delete flow).
- **Cancel a live run** before/while spinning down: `LedgerStore(pid).set_run_state(cancel_requested=True)` (graceful, at next turn boundary). Soft spin-down doesn't need to WAIT for drain (pausing + channel archive are independent), it just requests cancel.
- **Archive a Slack channel:** `web_client.conversations_archive(channel=cid)` — **net-new**;
  needs `channels:manage` (already held); treat `already_archived` as success; surface
  `cant_archive_general`/`missing_scope` as a clean error. Duck-type the Slack error code
  like `provisioning._slack_error_code`.
- **No project→channel reverse lookup** — bindings are keyed by channel. → add
  `store.channel_for_project(project_id)` (scan `_load_bindings()`).
- **Reconfigure team:** `control_actions.assign_models_by_role(store, role_routes, *, available, surface="pop")`.
  `role_routes` = coding role (`pm|dev|reviewer|tester`) → **human model name** (e.g.
  `{"reviewer":"opus"}`); resolved grounded-or-refuse against `available`; edits members'
  `gateway_route_id` via `set_run_config` and records an **undoable PmChange**. Works
  **mid-run** (no need to stop). `available = pm_reference.list_available_routes()`
  (fail-open-empty → a clean "no models available" refusal; can be slow probing Ollama —
  compute once per turn, INJECT in tests).

## 3. Design

### 3.1 Spin-down — `archive_project` (C-class, studio)

New studio verb (`studio_tools.py`). It is **destructive-ish (removes a channel + pauses
the project)** → **C-class**: only a verified Slack button executes it; chat text stages a
confirmation. Effect (soft, reversible):
1. Resolve the project's channel: `store.channel_for_project(project_id)`.
2. If a run is live, request cancel: `ledger_factory(project_id).set_run_state(cancel_requested=True)`.
3. `ledger_factory(project_id).set_project_status("paused")`.
4. `provisioning.archive_channel(web_client, channel_id)` (idempotent).
5. `store.unbind(channel_id)`.
6. Return `{"status":"archived","project_id","channel_id"}`. If no channel is bound,
   still pause + report (the project is spun down; there just wasn't a channel).
Error handling: `cant_archive_general`/`missing_scope`/other → `{"status":"error",...}`
naming what failed (project may already be paused — that's fine); never an uncaught raise.

**Hard delete is DEFERRED** (needs cancel→drain→`DELETE /projects/{id}`); note in §6.

### 3.2 Reconfigure — `reconfigure_team` (R-class, per-project)

New per-project verb (`tools.py`), exercised in the project's channel ("switch the
reviewer to opus"). It's reversible (undoable PmChange), doesn't directly spend, and
matches the app's own PM control-action path → **R-class** (applies immediately, reply
announces the change). Effect:
- `available = deps.available_routes or pm_reference.list_available_routes()` (injected in
  tests; the default call may probe — call once).
- `control_actions.assign_models_by_role(deps.ledger_factory(project_id), role_routes, available=available)`.
- Grounded-or-refuse: a `ControlActionError` (`model_not_found`/`model_ambiguous`/
  `no_matching_members`) → a clean `{"status":"error","detail":<reason + candidates>}` the
  PM relays ("I don't have a model matching 'X' — available: …"), NOT a crash.
- `role_routes` comes from the concierge's parsed args (role→name). Return
  `{"status":"reconfigured","changes":<role→route>}`.

### 3.3 Concierge grounding updates (important)

Both concierge prompts currently assert negatives that are now FALSE:
- Studio (`studio_concierge.py`): "NO tool to edit, rename, delete, or reconfigure a
  project." → remove/soften: it now CAN `archive_project` (spin down). Keep the truthful
  parts (it still can't rename, or hard-delete).
- Per-project (`concierge.py` grounding rule): "NO tool to … configure a team." → it now
  CAN `reconfigure_team`. Update so the grounding stays accurate (the "what I can do" list
  already derives from the catalog; fix the explicit negatives so they don't contradict it).
Both catalogs' anti-drift canaries must stay green; the negatives are the only hand-written
copy to reconcile.

## 4. Trust / injection

- `archive_project` = **C** — verified button only; chat text can't spin a project down.
  Same staged-confirmation discipline + tests as `create_project`/`start_run`.
- `reconfigure_team` = **R** — executes immediately (reversible, allowlist-gated); reply
  announces the change. (If we later want it gated, bump to C — one-line change.)
- Allowlist + optionality unchanged; no `slack_sdk` at module load; injected deps only.

## 5. Testing (egress-free)

- `store.channel_for_project`: returns the bound channel for a project; None if unbound;
  independent of the studio-channel singleton.
- `provisioning.archive_channel`: calls `conversations_archive`; `already_archived` →
  success; `cant_archive_general`/`missing_scope` → `ProvisioningError.code`; injected fake client.
- `archive_project` (studio): **injection** — `confirmed_via=None` → needs_confirmation,
  no archive/unbind/status-write; `"block_actions"` → cancels run (if live) + pauses +
  archives channel + unbinds, in order; no-channel-bound path pauses + reports.
- `reconfigure_team` (per-project): a valid `{"reviewer":"opus"}` against injected
  `available` → `assign_models_by_role` applied (members' route changed); an unavailable/
  ambiguous model → clean error with candidates, no mutation; monkeypatch/inject
  `available` so no CLI/Ollama probe in tests.
- Both anti-drift canaries green with the new verbs; trust classes asserted
  (`archive_project`="C", `reconfigure_team`="R"). Optionality holds.

## 6. Deferred

- **Hard delete** (`DELETE /projects/{id}` full teardown) — needs cancel→drain→delete;
  a separate, extra-confirmed "destroy" verb later. Soft spin-down (pause + archive) is
  the reversible default here.
- Un-archive / re-open a spun-down project from Slack (the inverse) — later.
- Per-channel access control, `#slug` addressing, digests (Slice 4 / polish).
