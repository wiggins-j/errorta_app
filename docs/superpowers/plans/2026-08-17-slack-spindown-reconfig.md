# Slack Spin-Down + Reconfigure (Slice 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Spin a project down (pause + archive its Slack channel + unbind) from the studio manager, and reconfigure a project's team (role→model) from the project channel.

**Architecture:** `archive_project` — a C-class studio verb (verified button only) doing a reversible soft spin-down. `reconfigure_team` — an R-class per-project verb over `assign_models_by_role` (grounded-or-refuse, undoable, mid-run safe). Plus a channel-archive plumbing call and a project→channel reverse lookup.

## Global Constraints
- **Injection guard:** `archive_project` executes ONLY with `confirmed_via="block_actions"`. (Spec §4.)
- **Soft, reversible spin-down** (pause + archive channel + unbind) — NO hard delete this slice. (Spec §2/§6.)
- Both concierge prompts' hand-written "no such tool" negatives MUST be reconciled so they don't contradict the new catalogs; the anti-drift canaries stay green. (Spec §3.3.)
- No `slack_sdk` at module load; injected deps only (web_client, ledger_factory, available_routes); no CLI/Ollama probe in tests. PUBLIC repo; placeholders.
- Run: `( cd python && .venv/bin/python -m pytest tests/slack tests/coding -q )`; ruff clean.

---

## Task 1: `store.channel_for_project` + `provisioning.archive_channel`

**Files:** Modify `python/errorta_slack/store.py`, `python/errorta_slack/provisioning.py`; Test `tests/slack/test_store.py`, `tests/slack/test_provisioning.py`.

**Produces:**
- `store.channel_for_project(project_id: str) -> str | None` — scan `_load_bindings()` for the channel whose binding's `project_id` matches; None if none. Add to `__all__`.
- `provisioning.archive_channel(web_client, channel_id: str) -> dict` — `web_client.conversations_archive(channel=channel_id)`; return `{"channel_id","archived":True}`; on a Slack error whose code is `already_archived` → treat as success (`archived:True`); on `cant_archive_general`/`missing_scope`/other create-step failure → raise `ProvisioningError(code, ...)`. Duck-type the error code via the existing `_slack_error_code`.

- [ ] **Step 1: Failing tests** — `channel_for_project`: bind `("C1","p1")`,`("C2","p2")` → `channel_for_project("p1")=="C1"`; unknown → None; a studio-channel set doesn't confuse it. `archive_channel`: fake client records `conversations_archive(channel="C1")` → `{"channel_id":"C1","archived":True}`; a fake raising `already_archived` → still `archived:True`; `cant_archive_general` → `ProvisioningError` code; `missing_scope` → `ProvisioningError` code.
- [ ] **Step 2–4:** Run FAIL → implement (mirror existing store/provisioning patterns) → Run PASS.
- [ ] **Step 5: Commit** `feat(slack): channel_for_project + archive_channel`.

---

## Task 2: `archive_project` (C) studio verb + studio prompt

**Files:** Modify `python/errorta_slack/studio_tools.py`, `python/errorta_slack/studio_concierge.py`; Test `tests/slack/test_studio_tools.py`, `tests/slack/test_studio_concierge.py`.

**Consumes:** `store.channel_for_project`, `store.unbind`, `provisioning.archive_channel` (T1); `deps.ledger_factory` (set_run_state/set_project_status); `deps.web_client`.

**Produces:** `TOOL_CATALOG` gains `archive_project` (trust "C"). `_VERB_IMPLS["archive_project"]` (confirmed path): `cid = store.channel_for_project(project_id)`; if the run is live (`ledger_factory(pid).get_run_state().get("status")=="running"`) → `set_run_state(cancel_requested=True)`; `ledger_factory(pid).set_project_status("paused")`; if `cid`: `provisioning.archive_channel(deps.web_client, cid)` then `store.unbind(cid)`; return `{"status":"archived","project_id":pid,"channel_id":cid}`. Errors (ProvisioningError etc.) → `{"status":"error","project_id":pid,"detail":...}`, no crash. Update `studio_concierge.build_system_prompt` grounding copy: it now CAN spin a project down (archive) — remove the "NO tool to delete/archive" negative, keep truthful ones (no rename, no hard-delete). Keep the `assert set(_VERB_IMPLS)==set(TOOL_CATALOG)`.

- [ ] **Step 1: Failing tests** — **injection:** `dispatch("archive_project", {project_id:"p1"}, confirmed_via=None)` → needs_confirmation, NO archive/unbind/status-write (fakes call-count 0); `confirmed_via="block_actions"` → set_project_status("paused") + archive_channel(cid) + unbind(cid) all called; a running project also gets `set_run_state(cancel_requested=True)`; a project with no bound channel → paused + reported, no archive/unbind. ProvisioningError from archive → clean `error`. Prompt test: build_system_prompt no longer claims it can't spin down; catalog canary still green.
- [ ] **Step 2–4:** Run FAIL → implement (mirror `create_project`'s C-class staging) → Run PASS.
- [ ] **Step 5: Commit** `feat(slack): archive_project studio verb (spin-down)`.

---

## Task 3: `reconfigure_team` (R) per-project verb + concierge grounding

**Files:** Modify `python/errorta_slack/tools.py`, `python/errorta_slack/concierge.py`; Test `tests/slack/test_tools.py`, `tests/slack/test_concierge.py`.

**Consumes:** `errorta_council.coding.control_actions.assign_models_by_role`, `pm_reference.list_available_routes` (injectable via `deps.available_routes`).

**Produces:** `TOOL_CATALOG` gains `reconfigure_team` (trust "R"). `_VERB_IMPLS`: `available = deps.available_routes if deps.available_routes is not None else pm_reference.list_available_routes()` (import inside); `assign_models_by_role(deps.ledger_factory(project_id), role_routes, available=available)`; return `{"status":"reconfigured","changes":role_routes}`. A `control_actions.ControlActionError` (model_not_found/ambiguous/no_matching_members) → `{"status":"error","detail":<message incl. candidates>}`, no crash. `role_routes` (dict role→name) comes from `args`. `ToolDeps` gains `available_routes: list | None = None`. Update `concierge.py` grounding copy: it now CAN reconfigure a team (change role→model) — reconcile the "no configure-a-team tool" negative. Keep the catalog/dispatch assert.

- [ ] **Step 1: Failing tests** — valid `{"role_routes":{"reviewer":"opus"}}` against injected `available=[{route_id:"claude_cli.opus",family:"opus",provider_class:"claude_cli"}, ...]` → `assign_models_by_role` applied (a fake/real store shows the reviewer's `gateway_route_id` changed); an unavailable model → clean `error` with candidates, no mutation; `no_matching_members` → clean error. Trust: `reconfigure_team`="R". Prompt test: concierge grounding no longer claims it can't reconfigure; canary green. (Use `assign_models_by_role` against a real tmp-ERRORTA_HOME project with a team + injected `available`, mirroring `test_f145_control_actions.py`.)
- [ ] **Step 2–4:** Run FAIL → implement → Run PASS.
- [ ] **Step 5: Commit** `feat(slack): reconfigure_team per-project verb`.

---

## Task 4: docs + canaries + gate

**Files:** Modify `docs/SLACK_STUDIO.md` (spin-down) + `docs/SLACK_PM_BRIDGE.md` (reconfigure); Test: the two catalog canaries.

- [ ] **Step 1:** Confirm `tests/slack/test_studio_catalog_canary.py` + `tests/slack/test_catalog_canary.py` green with the new verbs (reconcile prompt copy if not).
- [ ] **Step 2:** Docs: SLACK_STUDIO.md — "Spin a project down" (Approve button → pause + archive channel; reversible; hard delete not yet). SLACK_PM_BRIDGE.md — "Reconfigure the team" ("switch the reviewer to opus"; grounded-or-refuse; undoable; takes effect next turn). Placeholders only.
- [ ] **Step 3:** Full gate: `( cd python && .venv/bin/python -m pytest tests/slack -q )` green; `ruff check errorta_slack` clean; `import errorta_app.server` still doesn't pull `errorta_slack`; secret/PII grep on the diff.
- [ ] **Step 4: Commit** `docs(slack): spin-down + reconfigure`.

## Self-Review
- Spec §3.1 → T2; §3.2 → T3; §3.3 prompt updates → T2+T3; §2 plumbing → T1; §4 injection → T2 test; §5 → all; §6 deferred → not built. Types: `channel_for_project`, `archive_channel`→`{channel_id,archived}`, `available_routes` dep, trust classes consistent.
