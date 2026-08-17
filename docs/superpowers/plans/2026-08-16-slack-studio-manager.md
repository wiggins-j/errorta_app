# Slack Studio Manager (Slice 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A Slack "studio manager" that creates an Errorta coding project from a north star — project (runnable-by-construction) + team + an auto-created Slack channel, bound so the per-project PM is immediately live in it.

**Architecture:** A new app-level concierge bound to a designated **studio channel**, with its own bounded tool surface (`studio_tools`) whose only effectful verb, `create_project`, is C-class (fires only from a verified Slack button). It calls one clean engine seam — `project_factory.create_project_from_charter` — plus `provisioning` (Slack channel create/invite) and the existing binding store. Per-project channels keep their exact existing path.

**Tech Stack:** Python 3.10+, `slack_sdk` (WebClient conversations.* — new scope `channels:manage`), pytest + pytest-asyncio, the existing `errorta_council.coding` engine.

## Global Constraints

- **Injection guard (non-negotiable):** every effectful studio verb (`create_project`) executes ONLY when `dispatch(...)` is called with `confirmed_via="block_actions"` from a verified Slack interaction — NEVER from studio-concierge text. Untrusted Slack text can't create projects/channels. (Spec §5.)
- **`studio_tools` is the only door** from the studio concierge to app operations; unknown verb → fail-closed, named back (mirror `errorta_slack/tools.py`).
- **One engine seam:** `errorta_slack` reaches the coding engine only through `project_factory.create_project_from_charter` — never re-implement the compose steps. (Spec §3.)
- **Optionality:** no `slack_sdk` import at module load in any `errorta_slack` module; nothing in the core boot path imports `errorta_slack`; `project_factory` has NO slack dependency. (Spec §5.)
- **Public repo:** no tokens/PII; tests use placeholders (`xoxb-…`, `U…`, `T…`, `C…`).
- **Greenfield create:** `target="new"`, `repo_path=None` — no git path needed. (Spec §2.1.)
- **Slice 1 leaves the project IDLE** — no run start. (Spec §1.)
- Tests: `python/tests/slack/` and `python/tests/coding/`. Run: `( cd python && .venv/bin/python -m pytest tests/slack tests/coding -q )`. Merge gate: `( cd python && .venv/bin/python -m pytest )`.

---

## Reference: verified engine signatures (copy exactly)

```python
# errorta_council/coding/ledger.py
LedgerStore(project_id).create_project(*, north_star, definition_of_done,
    target, repo_path, delivery_root=None, work_request="", import_source=None) -> Project
LedgerStore(project_id).set_run_config(**patch)      # merge-write run-config.json; use room_id=None, members=[...]
list_projects(root=None) -> list[dict]               # cross-project list

# errorta_council/coding/workspace.py
CodingWorkspace(project_id, store).setup(target="new", repo_path=None)   # greenfield auto-seeds a tempdir worktree

# errorta_council/coding/governance.py
GovernanceStore.for_ledger(store).append_artifact(kind="brainstorm", title=..., body_markdown=..., body_json=charter, state="approved", author={"role":"pm","id":"studio"})
# + gov.update_state(**recipes.governance_overrides(recipe, autonomous))

# errorta_council/coding/autonomy.py
load_policy(store); save_policy(store, policy); policy_from_dict(d); policy_to_dict(p)
# errorta_council/coding/recipes.py
resolve_team(recipe, pm_reference.list_available_routes()) -> list[member dict]  # 2 dev+1 rev+1 pm, [] if no routes
governance_overrides(recipe, autonomous) -> dict ; autonomy_overrides(recipe, autonomous) -> dict
# errorta_council/coding/pm_reference.py
list_available_routes() -> list[{route_id, family, provider_class}]   # ⚠️ can shell out; INJECT in tests

# member dict shape:
{"id": "pm-1", "role": "answerer", "enabled": True, "model_mode": "single",
 "metadata": {"coding_role": "pm|dev|reviewer|tester"}, "gateway_route_id": "<route>"}

# Canonical create-on-accept to MIRROR (Tauri-gated, do not call): routes/coding.py::wizard_create() steps 1-5.
# Route-private helper to replicate: _set_run_setup_confirmed(store, True) — read it, replicate its effect.

# slack_sdk.WebClient (inject a fake in tests):
wc.auth_test()["user_id"]                                   # bot user id
wc.conversations_create(name=<slug>, is_private=False)["channel"]["id"]
wc.conversations_invite(channel=<cid>, users=[<uid>,...])
wc.conversations_setTopic(channel=<cid>, topic=<str>)
wc.chat_postMessage(channel=<cid>, text=<str>, blocks=<opt>)

# errorta_slack/store.py — existing: bind_channel(cid, pid), binding_for(cid), list_bindings(); atomic 0600 _read_json/_write_json/_LOCK
# errorta_slack/connection.py — handle_event (~169) resolves binding_for → project; _process (~384) branches on project_id
# Test templates: tests/coding/test_f145_pm_parity.py (create+team+injected routes); tests/slack/test_preflight.py::_FakeWebClient; tests/slack/test_connection.py fakes
```

---

## Task 1: `project_factory.create_project_from_charter`

**Files:**
- Create: `python/errorta_council/coding/project_factory.py`
- Test: `python/tests/coding/test_project_factory.py`

**Interfaces:**
- Produces: `create_project_from_charter(project_id: str, charter: dict, *, delivery_root: str | None = None, available_routes: list | None = None, members: list | None = None) -> Project`
  - `charter` keys: `north_star, definition_of_done, audience, modality, entrypoint, scope_notes, team_recipe, autonomous`.
  - Composes (mirror `wizard_create` steps 1-5): create_project(target="new", repo_path=None) → CodingWorkspace.setup(target="new", repo_path=None) → GovernanceStore brainstorm artifact (state="approved", body_json=charter) + governance_overrides → autonomy save_policy(merged) → team (explicit `members`, else `resolve_team(charter["team_recipe"], available_routes or pm_reference.list_available_routes())`); if team non-empty → `set_run_config(room_id=None, members=...)` + replicate `_set_run_setup_confirmed(store, True)`.
  - Raises `ValueError`/`LedgerError` on unsafe project_id or missing required charter fields. NO slack import.

- [ ] **Step 1: Failing test** — with a tmp `ERRORTA_HOME` (fixture like `test_f145_pm_parity`) and `available_routes` injected (e.g. `[{"route_id":"local.qwen","family":"qwen","provider_class":"local"}]`), call `create_project_from_charter("hs-game", {charter...}, available_routes=ROUTES)`; assert: `LedgerStore("hs-game").get_project()` exists with the north_star/DoD; the workspace dir exists; `GovernanceStore.for_ledger(store)` has an approved `brainstorm` artifact whose `body_json` == charter; `store.get_run_config()["members"]` is non-empty. Also a test that an explicit `members=[...]` list is applied verbatim. Also: unsafe id (`"../x"`) raises; missing `north_star` raises.
- [ ] **Step 2: Run** → FAIL (module missing).
- [ ] **Step 3: Implement** by reading `routes/coding.py::wizard_create()` (~2103-2132) and `_set_run_setup_confirmed`, and replicating steps 1-5 as pure library code. Import inside the function where needed to avoid heavy import cost. Do NOT import anything from `errorta_app.routes` or `errorta_slack`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(coding): create_project_from_charter — reusable runnable create`.

Note (deferred, not this task): refactoring `wizard_create()` to call this helper (DRY) is a follow-up; do NOT change the route in Slice 1.

---

## Task 2: Studio-channel binding in `store.py`

**Files:**
- Modify: `python/errorta_slack/store.py`
- Test: `python/tests/slack/test_store.py`

**Interfaces:**
- Produces (new, persisted to `studio.json` under `config.slack_dir()`, atomic 0600, `_LOCK`): `set_studio_channel(channel_id: str) -> None`, `studio_channel() -> str | None`, `is_studio(channel_id: str) -> bool`, `clear_studio_channel() -> None`. Add to `__all__`.

- [ ] **Step 1: Failing test** — `studio_channel()` is None initially; `set_studio_channel("C1")` then `studio_channel()=="C1"` and `is_studio("C1")` True, `is_studio("C2")` False; `clear_studio_channel()` → None; the studio setter does NOT disturb `bindings.json` (bind a project channel, set studio, assert both readable).
- [ ] **Step 2–4:** Run FAIL → implement mirroring the `bindings.json` pattern (`_read_json`/`_write_json`/`_LOCK`) → Run PASS.
- [ ] **Step 5: Commit** `feat(slack): studio-channel binding in store`.

---

## Task 3: Channel provisioning (`provisioning.py`)

**Files:**
- Create: `python/errorta_slack/provisioning.py`
- Test: `python/tests/slack/test_provisioning.py`

**Interfaces:**
- Produces:
  - `derive_channel_name(title: str) -> str` — lowercase; replace runs of non-`[a-z0-9_-]` with `-`; strip/collapse `-`; ≤80; fallback `"proj"` if empty.
  - `create_project_channel(web_client, *, title: str, invite_user_ids: list[str], purpose: str = "") -> dict` — returns `{"channel_id": str, "name": str}`; calls `conversations_create(name=derive_channel_name(title), is_private=False)`, on `SlackApiError` with `name_taken` retries with a numeric suffix (bounded, e.g. up to `-9`), then `conversations_invite(channel, users=invite_user_ids)` (best-effort per user), then `conversations_setTopic`/`setPurpose` if purpose. `web_client` is injected (real WebClient in prod, fake in tests).

- [ ] **Step 1: Failing tests** — `derive_channel_name("Homeschool Game!")=="homeschool-game"`; `derive_channel_name("  ")=="proj"`; truncation to 80. With a fake web_client whose `conversations_create` returns `{"channel":{"id":"C9","name":"homeschool-game"}}`, `create_project_channel` returns `{"channel_id":"C9",...}` and called invite with the user ids. A fake whose first `conversations_create` raises `SlackApiError(name_taken)` then succeeds → retried with a suffixed name. A fake raising `missing_scope` → the error propagates as a clear typed error (so the caller can message the user).
- [ ] **Step 2–4:** Run FAIL → implement (no `slack_sdk` import at module load — accept `SlackApiError` duck-typed or import inside functions) → Run PASS.
- [ ] **Step 5: Commit** `feat(slack): Slack channel provisioning`.

---

## Task 4: Studio tool surface (`studio_tools.py`)

**Files:**
- Create: `python/errorta_slack/studio_tools.py`
- Test: `python/tests/slack/test_studio_tools.py`

**Interfaces:**
- Consumes: `project_factory.create_project_from_charter` (T1), `store` studio + `bind_channel` (T2 + existing), `provisioning.create_project_channel` (T3).
- Produces:
  - `TOOL_CATALOG: dict[str, {"trust":"R"|"C","summary":str}]` = `list_projects`(R), `create_project`(C), `answer_question`(R).
  - `class ToolError(Exception)` (`.code`); `StudioDeps` dataclass `{store, ledger_factory=LedgerStore, web_client=None, create_fn=project_factory.create_project_from_charter, list_projects_fn=ledger.list_projects, invite_user_ids=[]}` (all injectable).
  - `dispatch(verb, args, *, channel_id, thread_ts, confirmed_via=None, deps) -> dict` — unknown verb → `ToolError("tool_not_allowed")` naming the catalog; `create_project` with `confirmed_via != "block_actions"` → returns `{"status":"needs_confirmation","confirmation_id":...}` via `deps.store.stage_confirmation`, does NOT create; with `"block_actions"` → runs create_fn → create_project_channel → `store.bind_channel(new_channel, project_id)` → returns `{"status":"created","project_id","channel_id"}`. `list_projects` → `{"projects":[...]}`. Errors from provisioning surface as a result dict (project created + channel-failed reason), never an uncaught raise.

- [ ] **Step 1: Failing tests** (the load-bearing ones):
  - **Injection:** `dispatch("create_project", {charter...}, confirmed_via=None)` returns `needs_confirmation` and `create_fn`/`web_client` are NEVER called; `dispatch(..., confirmed_via="block_actions")` DOES call `create_fn` then `create_project_channel` then `store.bind_channel`. (Inject fakes recording calls.)
  - **Fail-closed:** `dispatch("delete_all", {})` raises `ToolError("tool_not_allowed")` naming catalog verbs.
  - `list_projects` returns the injected `list_projects_fn()` output shape.
  - **Channel-fail path:** a `create_project_channel` that raises `missing_scope` after `create_fn` succeeded → dispatch returns `{"status":"error","project_id":<id>,"detail":...}` (project id preserved), no uncaught raise, and does NOT bind.
- [ ] **Step 2–4:** Run FAIL → implement (mirror `errorta_slack/tools.py` grant-or-refuse + staged-confirmation) → Run PASS.
- [ ] **Step 5: Commit** `feat(slack): studio tool surface with injection guard`.

---

## Task 5: Studio concierge (`studio_concierge.py`)

**Files:**
- Create: `python/errorta_slack/studio_concierge.py`
- Test: `python/tests/slack/test_studio_concierge.py`

**Interfaces:**
- Consumes: `studio_tools.dispatch`, `studio_tools.TOOL_CATALOG`.
- Produces:
  - `build_system_prompt(*, catalog=studio_tools.TOOL_CATALOG) -> str` — a charter-intake prompt (gather north_star/audience/modality/definition_of_done/entrypoint/team_recipe/autonomous) + the studio catalog + the SAME injection/grounding/etiquette contract as `concierge.py` (only tools listed; never claim/stage outside them; C-class needs a button).
  - `run_turn(message, thread_msgs, *, deps, caller, max_hops=2) -> {"reply","tool_results","reactions","assumed"}` — same JSON-envelope parse + robustness (malformed→one retry→graceful) as `concierge.run_turn`; ALWAYS dispatches with `confirmed_via=None` (never block_actions); `caller` injected.

- [ ] **Step 1: Failing tests** with a fake caller: a "create a homeschool game" message whose envelope emits `create_project` → `run_turn` calls `studio_tools.dispatch(..., confirmed_via=None)` → result is `needs_confirmation` (assert the create_fn is NOT executed). Malformed-JSON→retry→graceful. Unknown verb → graceful "here's what I can do". The **injection** test: an envelope emitting `create_project` from text never creates (confirmed_via stays None).
- [ ] **Step 2–4:** Run FAIL → implement mirroring `concierge.py` → Run PASS.
- [ ] **Step 5: Commit** `feat(slack): studio concierge (intake + injected caller)`.

---

## Task 6: Routing + interaction (`connection.py`)

**Files:**
- Modify: `python/errorta_slack/connection.py`
- Test: `python/tests/slack/test_connection.py`

**Interfaces:**
- Consumes: `store.is_studio` (T2), `studio_concierge.run_turn` (T5), `studio_tools.dispatch` (T4).
- Produces (changes to `SlackBridge`): a studio caller + web_client held on the bridge (injected via constructor, default None); `handle_event` tags `item["route"]="studio"` when `store.is_studio(channel_id)`; `_process` routes `route=="studio"` → `studio_concierge.run_turn(...)` then `_post_result`; `handle_interaction` routes a confirmed studio-`create_project` confirmation to `studio_tools.dispatch(..., confirmed_via="block_actions")` (the studio staged-confirmation path).

- [ ] **Step 1: Failing tests** — a message in the studio channel (fake `store.is_studio→True`) invokes `studio_concierge.run_turn`, NOT the per-project `concierge.run_turn`; a message in a project channel invokes the per-project `concierge`; an unbound non-studio channel → `_post_unbound`. A verified `block_actions` Approve for a studio `create_project` confirmation calls `studio_tools.dispatch(..., confirmed_via="block_actions")`; a text "approve" never does. Existing routing/injection/allowlist tests stay green.
- [ ] **Step 2–4:** Run FAIL → implement the branch (keep the per-project path untouched; keep ack/dedupe/allowlist/bot-filter first) → Run PASS.
- [ ] **Step 5: Commit** `feat(slack): route studio channel to the studio manager`.

---

## Task 7: Wiring, route, manifest, docs

**Files:**
- Modify: `python/errorta_app/slack_lifecycle.py`, `python/errorta_slack/routes.py`, `docs/slack-app-manifest.yaml`
- Create: `docs/SLACK_STUDIO.md`
- Test: `python/tests/slack/test_routes.py`, `python/tests/slack/test_optionality.py`

**Interfaces:**
- Produces: `slack_lifecycle._start_locked` builds a studio caller (`gateway_member_caller(LocalGateway())` — the studio manager's own member, default route `claude_cli.opus`, resolved like a PM) and a provisioning `WebClient(bot_token)`, passing both into `SlackBridge`. `routes.py`: `POST /slack/studio/bind {channel_id}` → `store.set_studio_channel`; `GET /slack/studio` → current studio channel. `manifest`: add `channels:manage` to bot scopes + update the header comment. `SLACK_STUDIO.md`: what it is, `POST /slack/studio/bind`, the new scope + reinstall note, the create flow, v1 scope (idle project, public channel).

- [ ] **Step 1: Failing tests** — `POST /slack/studio/bind` sets and `GET /slack/studio` returns it; optionality: `import errorta_app.server` still does not import `errorta_slack` (subprocess check, as in the existing optionality test); studio modules don't import `slack_sdk` at load.
- [ ] **Step 2–4:** Run FAIL → implement (keep imports lazy/in-function; guard the WebClient build like the poster) → Run PASS. Grep the manifest/docs for forbidden strings (no real tokens/PII).
- [ ] **Step 5: Commit** `feat(slack): wire studio manager + /slack/studio route + manifest scope + docs`.

---

## Task 8: Anti-drift canary + full gate

**Files:**
- Create: `python/tests/slack/test_studio_catalog_canary.py`

- [ ] **Step 1:** Canary asserting the verbs the studio prompt advertises (`studio_concierge.build_system_prompt`) == the verbs `studio_tools.dispatch` accepts (introspect the dispatch table). Fails on drift.
- [ ] **Step 2:** Run → PASS (reconcile if not).
- [ ] **Step 3: Commit** `test(slack): studio catalog anti-drift canary`.

---

## Final integration gate (after Task 8)

- [ ] `( cd python && .venv/bin/python -m pytest tests/slack tests/coding -q )` green; then the whole suite for regressions.
- [ ] `ruff check python/errorta_slack python/errorta_council/coding/project_factory.py` clean.
- [ ] Secret/PII grep over the branch diff (no real tokens, no owner email/home/hostnames).
- [ ] REQUIRED: superpowers:requesting-code-review (whole-branch, most capable model) before merge.

## Self-Review (author)

- **Spec coverage:** §3 layout → all tasks; §4.1 helper → T1; §4.2 concierge+tools → T4/T5; §4.3 provisioning → T3; §4.4 routing/wiring/route/manifest → T2/T6/T7; §5 injection/optionality → T4/T5/T6/T7 tests; §6 error handling → T3/T4 (channel-fail, missing_scope, no-routes); §7 testing → every task; §8 deferred → not built (correct).
- **Placeholder scan:** none — concrete signatures + representative tests throughout.
- **Type consistency:** `create_project_from_charter`, `StudioDeps`, `confirmed_via="block_actions"`, `TOOL_CATALOG`, `is_studio/studio_channel`, `create_project_channel`→`{"channel_id","name"}` used consistently across T1/T3/T4/T6.
- **Gap closed:** `_set_run_setup_confirmed` is route-private — T1 explicitly replicates its effect (read it first).
