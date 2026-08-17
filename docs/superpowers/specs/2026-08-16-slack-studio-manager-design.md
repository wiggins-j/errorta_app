# Slack Studio Manager — Design (Slice 1: create-a-project from Slack)

**Date:** 2026-08-16
**Status:** Design approved (brainstorm). Not yet planned/implemented.
**Depends on:** the merged Slack PM bridge (`python/errorta_slack/`) and the
concierge→PM-member wiring.
**Module:** extends `python/errorta_slack/` + one new helper in
`python/errorta_council/coding/`.

---

## 1. Problem & vision

Today the Slack bridge binds **one channel to one existing project** and you chat
that project's PM. You cannot create, list, or manage projects from Slack — and
when asked, the PM has no tool for it (it recently *confabulated* a "project
created" flow; now grounded to refuse). The vision the owner wants:

> A top-level PM you talk to in Slack that **operates the whole app** — spin
> projects up and down, set up teams, and drop into a conversation with any
> individual project's PM.

### Architecture (approved): channel-per-project + a studio manager

- A designated **studio channel** hosts the **studio manager** — a new app-level
  concierge. You talk to it to create/list/(later)manage projects.
- **Each project gets its own Slack channel**, auto-created by the studio manager,
  where you talk to *that project's PM* — the per-project bridge that already
  ships and was live-tested. Spinning a project down (later slice) archives its
  channel.
- Chosen over single-channel/thread-per-project because the owner expects **many
  projects and shared access**: named/findable channels, prominent decisions, and
  per-channel access control age better than tucked-away threads.

### This spec is Slice 1 only

Decomposed; later slices are **out of scope here**:

- **Slice 1 (this doc): create-a-project.** Studio manager takes a north star →
  creates the Errorta project (runnable-by-construction) + team config + a Slack
  channel + binds them + posts the project channel's intro. **Project is left
  IDLE & ready** — starting the coding run is Slice 2.
- Slice 2: run control (start/stop/pause).
- Slice 3: cross-project ops (archive/spin-down, team reconfig).
- Slice 4: access control, `#slug` addressing, digests.

---

## 2. Key grounded facts (from the mechanics investigation)

1. **Greenfield create needs no git path.** `LedgerStore.create_project(*, north_star,
   definition_of_done, target, repo_path, ...)` with `target="new"`, `repo_path=None`;
   `CodingWorkspace(pid, store).setup(target="new", repo_path=None)` auto-seeds an
   isolated worktree from a tempdir. No user filesystem input required.
2. **No single below-HTTP callable creates a runnable project from a charter.** The
   canonical composition is `routes/coding.py::wizard_create()` (5 steps), but it is
   `_require_tauri_origin`-gated → **not** callable from the Slack sidecar. So we
   **extract the composition into a reusable library helper** (§4.1).
3. **Team** = `store.set_run_config(room_id=None, members=[...])`. `create_project`
   seeds no default team. Member shape: `{"id","role":"answerer","enabled":True,
   "model_mode":"single","metadata":{"coding_role":<pm|dev|reviewer|tester>},
   "gateway_route_id":<route>}`.
4. **Slack provisioning** uses `WebClient.conversations_create / conversations_invite
   / conversations_setTopic / chat_postMessage` — needs new bot scope
   **`channels:manage`**. Channel names: lowercase, `[a-z0-9_-]`, ≤80, unique.
5. **Errorta project-id** charset `^[A-Za-z0-9._-]{1,64}$`, never `.`/`..` — distinct
   from Slack channel-name rules; derive the two identifiers separately.
6. **Routing seam:** `connection.handle_event`/`_process` resolve `binding_for` →
   project. Add a **studio binding** and branch to a studio concierge.

---

## 3. Components (module layout)

New / changed files:

```
errorta_council/coding/
  project_factory.py     # NEW: create_project_from_charter(...) — the reusable 5-step composition
  (routes/coding.py       # OPTIONAL refactor: wizard_create() calls the helper — DRY, low-risk-if-done)

errorta_slack/
  studio_concierge.py    # NEW: the studio manager turn — intake + its own tool surface + injection discipline
  studio_tools.py        # NEW: studio tool surface (list_projects, create_project[C], answer_question); ONLY door to app ops
  provisioning.py        # NEW: Slack channel provisioning (create/invite/topic) + name derivation; injected WebClient factory
  store.py               # CHANGED: add studio-channel singleton (studio.json): set_studio_channel/studio_channel/is_studio
  connection.py          # CHANGED: route studio-channel messages → studio_concierge; route studio [C] confirmations
  routes.py              # CHANGED: add POST /slack/studio/bind (owner sets which channel is the studio channel)
  config.py / secrets.py # unchanged

errorta_app/
  slack_lifecycle.py     # CHANGED: build the studio caller + a provisioning WebClient; pass into SlackBridge

docs/
  slack-app-manifest.yaml # CHANGED: add channels:manage scope (+ update the header comment)
  SLACK_STUDIO.md         # NEW: what the studio manager is + how to set the studio channel + scopes
```

**Design principle preserved:** `errorta_slack` reaches the coding engine only
through **one clean seam** — `project_factory.create_project_from_charter` — never
by re-implementing the 5-step composition. `studio_tools` is the only door from the
studio concierge to app operations (grant-or-refuse, injection guard).

---

## 4. How it works

### 4.1 `create_project_from_charter` (the reusable helper)

New `errorta_council/coding/project_factory.py`:

```python
def create_project_from_charter(
    project_id: str, charter: dict, *,
    delivery_root: str | None = None,
    available_routes: list[dict] | None = None,   # inject for tests; defaults to pm_reference.list_available_routes()
    members: list[dict] | None = None,            # explicit team; if None, recipes.resolve_team(charter["team_recipe"], routes)
) -> "Project":
    """Compose the runnable-by-construction create (mirrors wizard_create steps 1-5):
    1. LedgerStore(project_id).create_project(north_star, definition_of_done, target="new", repo_path=None, delivery_root)
    2. CodingWorkspace(project_id, store).setup(target="new", repo_path=None)
    3. GovernanceStore seed: append 'brainstorm' artifact (state=approved, body_json=charter) + governance_overrides
    4. autonomy: save_policy(merged autonomy_overrides)
    5. team: members (explicit) or recipes.resolve_team(...); if non-empty → store.set_run_config(room_id=None, members) + mark run-setup confirmed
    Returns the created Project. Raises on invalid charter / unsafe project_id.
    """
```

- `charter` must carry the Wizard's required fields (`north_star`, `audience`,
  `modality`, `definition_of_done`, `entrypoint`, `team_recipe`, `autonomous`).
- **DRY:** refactor `wizard_create()` to call this helper for steps 1-5 (keeps the
  route and Slack path identical). If that refactor proves risky, the helper still
  ships and the studio uses it; the route refactor becomes a follow-up (note in plan).
- Fully unit-testable with a tmp `ERRORTA_HOME` and injected `available_routes`
  (mirror `tests/coding/test_f145_pm_parity.py`).

### 4.2 The studio concierge & tool surface

`studio_concierge.run_turn(message, thread_msgs, *, deps, caller, web_client, max_hops=2)`
mirrors the per-project `concierge.run_turn` exactly — same JSON envelope
(`{reply, tool_calls, assumed}`), same **injection invariant** (dispatch always
`confirmed_via=None`; only a verified button fires a **C**-class verb), same
grounded-or-refuse etiquette. Its system prompt is a **charter-intake** prompt
(gather the required charter fields conversationally) + the studio tool catalog.

`studio_tools` verb surface (Slice 1):

| Verb | Trust | Effect |
|------|:---:|--------|
| `list_projects` | R | cross-project list + status (reads `ledger.list_projects()`) |
| `create_project` | **C** | assemble charter → `project_factory.create_project_from_charter` → `provisioning.create_project_channel` → `store.bind_channel(new_channel, project_id)` → intro post. Confirmed by a verified button. |
| `answer_question` | R | grounded Q&A, no side effect |

`create_project` is **C-class**: it never executes from chat text. The studio
concierge *stages* it (returns `needs_confirmation` with the charter summary +
channel-to-be); the studio concierge/render posts an Approve button; the verified
`handle_interaction` callback fires the real create. This is the same
staged-confirmation discipline as the per-project bridge (injection boundary).

### 4.3 Channel provisioning

`provisioning.py`:
- `derive_channel_name(title) -> str` — lowercase, `[a-z0-9_-]`, collapse/strip,
  ≤80, fallback `proj-<shortid>`; on `name_taken`, append a numeric suffix.
- `create_project_channel(web_client, *, title, invite_user_ids, purpose) -> dict`
  — `conversations_create(name=…, is_private=False)`, `conversations_invite`,
  `conversations_setTopic/Purpose`, returns `{channel_id, name}`.
- **Injected `web_client`** (a real `slack_sdk.WebClient` in prod, a fake in tests —
  mirror `tests/slack/test_preflight.py::_FakeWebClient`). No network in tests.

### 4.4 Routing & wiring

- `store`: `set_studio_channel(cid)`, `studio_channel() -> str|None`,
  `is_studio(cid) -> bool` persisted in a new `studio.json` (atomic 0600, mirrors
  bindings). Studio channel is a singleton for Slice 1.
- `connection.handle_event`: after the existing bot/subtype/allowlist filters, if
  `store.is_studio(channel_id)` → tag `item["route"]="studio"` (leave `project_id`
  as-is). `_process`: `route=="studio"` → `studio_concierge.run_turn`; else the
  existing project path; else `_post_unbound`. Studio [C] confirmations resolve via
  the same verified `handle_interaction` path, dispatched to `studio_tools`.
- `slack_lifecycle`: build a studio caller (a `gateway_member_caller` — the studio
  manager's own model; default `claude_cli.opus` like a PM, configurable) and a
  provisioning `WebClient`, and pass them into `SlackBridge`.
- `routes.py`: `POST /slack/studio/bind {channel_id}` (owner action) → `set_studio_channel`.
  Documented in `SLACK_STUDIO.md`. (No auto-discovery of the studio channel in v1.)
- `manifest`: add `channels:manage`; update the "exactly what the bridge uses" comment.

---

## 5. Trust, security, optionality

- **Injection guard (non-negotiable):** `create_project` (and any effectful studio
  verb) executes ONLY from a verified Slack `block_actions` payload — never from
  studio-concierge text. Untrusted Slack text can't spin up projects or channels.
  Same discipline + tests as the per-project bridge (`confirmed_via` marker).
- **Grounded-or-refuse:** the studio manager only claims what its catalog allows;
  the anti-drift canary is extended to the studio catalog.
- **Allowlist:** studio-channel messages pass the same `auth.is_allowed` fail-closed
  check already enforced in `handle_event`.
- **Optionality preserved:** all new code lazy/guarded; nothing imports `slack_sdk`
  or `errorta_slack` at core boot; `channels:manage` only matters when the bridge is
  enabled. `create_project_from_charter` is plain library code (no Slack dep).
- **Public-repo hygiene:** no tokens/PII; docs use placeholders.

---

## 6. Error handling

- **Ordering (fixed):** `create_project_from_charter` (project) → `create_project_channel`
  (retry name on `name_taken`) → `store.bind_channel` → intro post. If channel
  provisioning fails *after* the project was created, do NOT silently swallow it:
  report clearly ("project `<id>` was created, but its Slack channel failed:
  `<reason>` — fix the scope/name and retry") so the created project is never lost
  or left invisibly unbound. Bind only after a channel exists.
- **No available routes** for the team: warn and create the project idle without a
  confirmed team (mirror `wizard_create`'s unconfigured branch), telling the user to
  configure models — don't fail the whole create.
- **Missing `channels:manage` scope:** the WebClient raises `missing_scope`; catch and
  tell the user to update the Slack app manifest + reinstall.
- **Malformed charter / studio model unavailable:** same graceful paths as the
  per-project concierge (fail-closed, "here's what I can do").

---

## 7. Testing

All egress-free (inject `web_client`, `caller`, `available_routes`, `ledger_factory`).

- **`create_project_from_charter`**: creates a runnable project + team from a charter
  with a tmp ERRORTA_HOME + injected routes (mirror `test_f145_pm_parity`); asserts
  project.json, workspace, governance brainstorm, and run-config members all land.
- **Injection**: `create_project` from studio-concierge TEXT never provisions/creates
  — only a verified `block_actions` callback does (assert the create helper + WebClient
  are untouched without the button). The #1 test.
- **Provisioning**: `create_project_channel` calls conversations_create/invite/setTopic
  on a fake WebClient with canned responses; `derive_channel_name` rules (lowercase,
  strip, truncate, fallback, collision suffix).
- **Routing**: a message in the studio channel routes to `studio_concierge`, a message
  in a project channel routes to the per-project `concierge`, an unbound channel →
  `_post_unbound` (extend `test_connection.py`).
- **Error paths**: channel `name_taken` retried; `missing_scope` → clean user error;
  no-available-routes → idle project + warning, not a crash.
- **Anti-drift canary**: studio catalog verbs the studio prompt advertises == verbs
  `studio_tools.dispatch` accepts.
- **Optionality**: `import errorta_app.server` still doesn't import `errorta_slack`;
  studio modules don't import `slack_sdk` at module load.
- Whole `tests/slack` + relevant `tests/coding` stay green; `ruff` clean.

---

## 8. Deferred (explicitly not Slice 1)

- Run start/stop/pause (Slice 2) — the created project stays idle.
- Archive/spin-down (archives the channel), team reconfig from Slack (Slice 3).
- Per-channel allowlists / shared access, `#slug` addressing, digests (Slice 4).
- Auto-discovery of the studio channel (v1: owner sets it via `POST /slack/studio/bind`).
- Private (`groups:write`) project channels — v1 creates public channels.
