# Slack PM Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional `errorta_slack` sidecar subsystem that lets a user chat with the Errorta coding-team PM in a Slack channel — status/Q&A, launch previews, queue bug tasks — with proactive decision/terminal posts, all over the real coding-team seams.

**Architecture:** A new `python/errorta_slack/` package, sibling to `errorta_mobile/`. A **Socket Mode** ingress (outbound WS, no public URL) feeds a stateless **concierge** LLM turn that maps chat → a bounded **tool surface** (`tools.py`) over the existing engine (`LedgerStore` / `team_log` / `attention` / `runtime_process` / `pm_changes`). An **outbound** poller diffs coding-team state and posts Block Kit. All egress seams (Slack client, model caller, runtime launcher) are injected → fully unit-testable without network. The subsystem is off by default and optional.

**Tech Stack:** Python 3.10+, FastAPI (routes facade only), `slack-sdk>=3.27` (Socket Mode, optional extra), pytest + pytest-asyncio (`asyncio_mode=auto`).

## Global Constraints

- **Public repo — zero secrets/PII in git.** No real tokens, keys, owner email, home paths, or private hostnames in source/tests/fixtures/docs. Tests use placeholders (`xoxb-…`, `you@example.com`, `/path/to/...`). (Spec §1 Public-repo hygiene.)
- **Strictly optional.** Ships as extra `errorta_app[slack]`; enable flag off by default; lazy imports; nothing in `errorta_council` / `errorta_cli` / core boot path imports `errorta_slack` at module load. Missing `slack-sdk` → feature unavailable, never a boot error. (Spec §1 Optionality.)
- **`tools.py` is the only door to the engine.** Concierge cannot touch the engine except through the fixed verb list; unknown verb → fail-closed, named back. (Spec §3.2, §4.)
- **Injection guard (non-negotiable):** `resolve_decision` and every **C**-class verb execute ONLY when invoked with a `confirmed_via="block_actions"` provenance marker set by the verified Slack interaction callback — NEVER from concierge text output. (Spec §4 ‡, §7.)
- **Coding-team seams, not council.** Status/decisions come from `LedgerStore` + `team_log.build_team_log` + `attention.list_open` + `pm_changes` + `publish_ledger`. Do NOT use the council event stream or `errorta_policy` F041 store. (Spec §4, §6.2.)
- **Secrets 0600.** Slack tokens live in a `0600` JSON under `${ERRORTA_HOME}/slack/`, modeled on `errorta_app/provider_keys.py`; `xoxb`/`xapp` added to log redaction. (Spec §7.1.)
- **v1 scope:** `launch_runtime` returns the loopback/LAN URL only (no public ingress); `queue_bugs` appends lightweight `todo` tasks. Public URL + governed spec artifacts are v2. (Spec §4.2, §10.)
- Tests live under `python/tests/slack/`. Run the suite with `( cd python && pytest tests/slack -q )`. The merge gate is `( cd python && pytest )`.

---

## Reference: verified engine signatures (copy exactly)

```python
# errorta_council/coding/ledger.py
class LedgerStore:
    def __init__(self, project_id: str, *, root: Path | None = None) -> None: ...
    def create_project(self, *, north_star: str, definition_of_done: str, ...) -> ...: ...
    def add_task(self, *, title: str, role: str, detail: str = "", task_type: str = "implementation",
                 difficulty_tier: str = "mid", ...) -> Task: ...   # role must be in _VALID_ROLES

# errorta_council/coding/team_log.py
def build_team_log(store: Any) -> list[dict[str, Any]]: ...

# errorta_council/coding/attention.py
def list_open(project_id: str, *, store: "LedgerStore | None" = None) -> list["AttentionSignal"]: ...
@dataclass(frozen=True)
class AttentionSignal:
    id: str; project_id: str; kind: str; blocking: bool; source: str; stage: str
    title: str; summary: str; state: str = "open"; ...

# errorta_council/coding/pm_reference.py
def build_pm_reference_context(project_id: str | None = None, *, store: Any = None) -> str: ...

# errorta_council/coding/turn_controller.py
def allowed_tools_for_role(role: str) -> tuple[str, ...]: ...   # the grant-or-refuse pattern to mirror

# errorta_council/coding/runtime_process.py  — start() binds LOOPBACK only; returns a RuntimeSession w/ allocated_ports
#   RuntimeProfileStore.for_ledger(LedgerStore(pid))  → profiles; None configured → empty-state

# member caller injection seam (concierge model call): Callable[[dict, str], str]  → returns model text

# errorta_app/provider_keys.py — 0600 JSON token-store pattern to model secrets.py on
# errorta_app/mobile_lifecycle.py — sync()/stop() singleton wired into server.py lifespan; the pattern for slack_lifecycle
# errorta_app/server.py:152 lifespan(app); :332 mobile boot; :432 mobile stop; :575+ app.include_router(...)
```

---

## Task 1: Package skeleton, config, packaging, gitignore

**Files:**
- Create: `python/errorta_slack/__init__.py`, `python/errorta_slack/config.py`
- Modify: `python/pyproject.toml` (optional extra `slack`; add `slack-sdk` to `dev`), `.gitignore`
- Test: `python/tests/slack/test_config.py`, `python/tests/slack/__init__.py`

**Interfaces:**
- Produces:
  - `config.SLACK_API_VERSION: int`
  - `config.slack_dir() -> Path` (`${ERRORTA_HOME}/slack/`, created on demand, mode 0700)
  - `config.load() -> dict` — returns `{"enabled": bool, "bindings": list[dict], "window": int, "timeout_minutes": int}`; defaults `enabled=False, bindings=[], window=20, timeout_minutes=30`
  - `config.save(cfg: dict) -> None` (atomic write, 0600)
  - `config.is_enabled() -> bool`

- [ ] **Step 1: Write failing test** — `test_config.py`: `load()` on a fresh temp `ERRORTA_HOME` returns `enabled False, window 20, timeout_minutes 30, bindings []`; `save` then `load` round-trips; `slack_dir()` exists with mode `0o700`. Use `monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))`.
- [ ] **Step 2: Run** `pytest tests/slack/test_config.py -v` → FAIL (module missing).
- [ ] **Step 3: Implement** `config.py` mirroring `errorta_mobile/config.py` atomic-write + `errorta_app.paths.errorta_home`. `__init__.py` sets `SLACK_API_VERSION = 1` and does NOT import optional deps at module load.
- [ ] **Step 4: Packaging** — in `pyproject.toml` `[project.optional-dependencies]` add `slack = ["slack-sdk>=3.27"]` and append `"slack-sdk>=3.27"` to `dev`. Add `errorta_slack` to the packages list if packages are enumerated. In `.gitignore` add `python/**/slack/` runtime state pattern (match existing mobile pattern).
- [ ] **Step 5: Run** the test → PASS. Then `pip install -e '.[dev]'` (installs slack-sdk for tests).
- [ ] **Step 6: Commit** `feat(slack): package skeleton, config, optional extra`.

---

## Task 2: Slack token store (`secrets.py`)

**Files:**
- Create: `python/errorta_slack/secrets.py`
- Test: `python/tests/slack/test_secrets.py`

**Interfaces:**
- Produces:
  - `secrets.save_tokens(app_token: str, bot_token: str) -> None` — writes `${ERRORTA_HOME}/slack/tokens.json` mode 0600
  - `secrets.load_tokens() -> dict | None` — `{"app_token","bot_token"}` or `None` if absent
  - `secrets.mask() -> dict` — same keys, values reduced to `"…<last4>"` (for GET routes)
  - `secrets.REDACTION_PATTERNS: list[re.Pattern]` — matches `xoxb-…`, `xapp-…`

- [ ] **Step 1: Failing test** — save `("xapp-1-AAA-bbb","xoxb-9-cccddd")`; `load_tokens()` round-trips; file mode is `0o600`; `mask()` returns `{"app_token":"…-bbb"[-…], "bot_token":"…cddd"}` (last-4 rule); `load_tokens()` on empty home → `None`; a `REDACTION_PATTERNS` sub applied to `"got xoxb-9-cccddd here"` yields no raw token.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** modeled on `provider_keys.py` (0600, atomic tmp+rename, never logs values).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Extend log redaction** — add the `xoxb`/`xapp` patterns to `errorta_model_gateway/redaction.py` (reference `secret_scan.py`); add a test asserting the gateway redactor now masks a Slack token.
- [ ] **Step 6: Commit** `feat(slack): 0600 token store + xoxb/xapp redaction`.

---

## Task 3: Durable bridge store (`store.py`)

**Files:**
- Create: `python/errorta_slack/store.py`
- Test: `python/tests/slack/test_store.py`

**Interfaces:**
- Produces (all atomic-write JSON under `slack_dir()`):
  - Bindings: `bind_channel(channel_id: str, project_id: str) -> None`, `binding_for(channel_id) -> dict | None`, `list_bindings() -> list[dict]`, `unbind(channel_id) -> None`
  - Outbound cursor: `get_cursor(channel_id) -> str | None`, `advance_cursor(channel_id, marker: str) -> None` (idempotent; marker = last-posted coding-state fingerprint)
  - Dedupe: `seen_event(event_id: str) -> bool` (returns True if already seen; records it; bounded LRU of last 512)
  - Confirmation records: `stage_confirmation(verb: str, args: dict, thread_ts: str) -> str` (returns id), `get_confirmation(cid) -> dict | None`, `resolve_confirmation(cid, decision: str) -> dict` (sets state), `pop_pending_older_than(...)` for timeouts
  - Prefs: `set_pref(channel_id, key, value) -> None`, `get_prefs(channel_id) -> dict`

- [ ] **Step 1: Failing tests** — bind/lookup/unbind round-trip; `seen_event` returns False then True for same id; cursor advance is idempotent (advancing to the same marker twice is a no-op, to a new marker updates); `stage_confirmation` → `get` returns `state="pending"`, `resolve_confirmation` flips to the decision; prefs round-trip.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** with the mobile atomic-write helper pattern; keep each concern in its own JSON file (`bindings.json`, `cursors.json`, `seen-events.json`, `confirmations.json`, `prefs.json`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(slack): durable store — bindings, cursor, dedupe, confirmations, prefs`.

---

## Task 4: Tool surface + grant-or-refuse + injection guard (`tools.py`)

**Files:**
- Create: `python/errorta_slack/tools.py`
- Test: `python/tests/slack/test_tools.py`

**Interfaces:**
- Consumes: `store` (Task 3); `LedgerStore`, `team_log.build_team_log`, `attention.list_open`, `pm_changes` (engine); an injected `launch_fn` and `caller` for testability.
- Produces:
  - `TOOL_CATALOG: dict[str, ToolSpec]` where `ToolSpec = {"trust": "R"|"C", "summary": str}` — the source of truth the concierge prompt renders (anti-drift canary compares against this).
  - `class ToolError(Exception)` with `.code`
  - `dispatch(verb: str, args: dict, *, channel_id: str, thread_ts: str, confirmed_via: str | None = None, deps: ToolDeps) -> dict` — returns a result dict; unknown verb → `ToolError("tool_not_allowed")` naming the catalog; a **C**-class or `resolve_decision` verb with `confirmed_via != "block_actions"` → returns `{"status":"needs_confirmation","confirmation_id":...}` (staged, NOT executed).
  - `ToolDeps` dataclass: `{store, ledger_factory=LedgerStore, launch_fn, publish_fn, pm_changes_mod}` (all injectable).
  - Verb implementations: `list_projects`, `switch_project`, `project_status`, `recent_activity`, `launch_runtime`, `stop_runtime`, `queue_bugs`, `answer_question`, `resolve_decision`, `spend_cloud`, `publish_pr`.

- [ ] **Step 1: Failing tests** (the load-bearing ones):
  - `project_status` for a bound project calls `build_team_log(store)` + `list_open(project_id)` (inject a fake `ledger_factory` returning a stub store; assert the result dict has `tasks`, `blockers`).
  - `queue_bugs(["a","b","c"])` calls `store.add_task` three times with `role="dev"`, `task_type="implementation"`, returns the new task ids.
  - **Injection guard:** `dispatch("publish_pr", {...}, confirmed_via=None)` returns `status=="needs_confirmation"` and does NOT call `publish_fn`; `dispatch("publish_pr", {...}, confirmed_via="block_actions")` DOES call `publish_fn`. Same for `resolve_decision` and `spend_cloud`.
  - **Fail-closed:** `dispatch("delete_everything", {})` raises `ToolError("tool_not_allowed")` whose message contains the real catalog verbs.
  - `launch_runtime` with an injected `launch_fn` returning loopback ports produces a result with a `url` that is loopback/LAN and a `note` that no public URL is offered; with `launch_fn` reporting no profile → `status=="empty"`, `launch_fn` not asked to start.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** dispatch with a static `_TRUST` map; mirror `allowed_tools_for_role`'s fail-closed rejection wording. `role` for `add_task` is `"dev"` (verify against `_VALID_ROLES`; adjust to the real valid value if the test surfaces a `LedgerError`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(slack): bounded tool surface with grant-or-refuse + injection guard`.

---

## Task 5: Concierge turn (`concierge.py`)

**Files:**
- Create: `python/errorta_slack/concierge.py`
- Test: `python/tests/slack/test_concierge.py`

**Interfaces:**
- Consumes: `tools.dispatch`, `tools.TOOL_CATALOG`, `pm_reference.build_pm_reference_context`; an injected `caller: Callable[[dict, str], str]` (member-caller seam).
- Produces:
  - `build_system_prompt(project_id, *, store, catalog=TOOL_CATALOG) -> str` — pm_reference context + catalog + Slack-etiquette contract (brevity, injection rules, hybrid trust).
  - `run_turn(message: str, thread_msgs: list[dict], *, project_id, deps, caller, max_hops=2) -> TurnResult` where `TurnResult = {"reply": str, "tool_results": list, "reactions": list[str], "assumed": bool}`. Parses the model's JSON envelope `{"reply","tool_calls":[{"verb","args"}]}`; executes R-class calls via `tools.dispatch`; folds results into one follow-up turn (bounded by `max_hops`); malformed JSON / unknown verb → one corrective retry then a plain "here's what I can do" reply. Sets `reactions=["🤔"]` when the model flags an assumption, `["✅"]` on a confident action.

- [ ] **Step 1: Failing tests** with a **fake caller** returning canned envelopes:
  - message "how's it going" → caller returns `{"reply":"...","tool_calls":[{"verb":"project_status","args":{}}]}` → `run_turn` calls dispatch and returns a reply; `tool_results` has the status.
  - caller returns malformed JSON on turn 1, valid on retry → `run_turn` recovers.
  - caller emits `{"verb":"nuke"}` → reply falls back to the "here's what I can do" text listing catalog verbs; no exception escapes.
  - an envelope with `"assumed": true` → `reactions` contains `🤔`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Never let a text-emitted `resolve_decision`/C-class verb execute — pass `confirmed_via=None` from concierge always (only the callback in Task 8 passes `"block_actions"`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(slack): concierge turn over injected model-caller`.

---

## Task 6: Block Kit rendering (`render.py`)

**Files:**
- Create: `python/errorta_slack/render.py`
- Test: `python/tests/slack/test_render.py`

**Interfaces:**
- Produces pure functions returning Block Kit dict lists:
  - `status_card(team_log: list, blockers: list) -> list[dict]`
  - `decision_message(title: str, detail: str, confirmation_id: str) -> list[dict]` — 🔴 header "DECISION NEEDED" + Approve/Decline buttons whose `action_id`/`value` carry `confirmation_id`
  - `fyi_message(text: str) -> list[dict]` — plain section, no buttons
  - `reactions_for(turn_result) -> list[str]`

- [ ] **Step 1: Failing tests** — `decision_message` output contains a `header` block with 🔴 text and two `button` elements whose `value == confirmation_id` and distinct `action_id`s (`slack_approve` / `slack_decline`); `fyi_message` has no `actions` block; `status_card` renders counts from a sample team_log.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (pure dict builders). **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(slack): Block Kit renderers`.

---

## Task 7: Auth + owner-confirmation linking (`auth.py`, `linking.py`)

**Files:**
- Create: `python/errorta_slack/auth.py`, `python/errorta_slack/linking.py`
- Test: `python/tests/slack/test_auth.py`, `python/tests/slack/test_linking.py`

**Interfaces:**
- Produces:
  - `auth.verify_signature(headers: dict, body: bytes, signing_secret: str) -> bool` — Slack v0 HMAC (`v0:timestamp:body`), constant-time compare, 5-min timestamp window.
  - `auth.is_allowed(team_id: str, user_id: str, cfg: dict) -> bool` — team + user allowlist; empty allowlist → deny.
  - `linking.request_link(channel_id, project_id, requester_user_id) -> str` (returns link_id, state `awaiting_owner`); `linking.approve_link(link_id) -> dict` (owner-side; binds channel via `store.bind_channel`, mints capability grant); `linking.deny_link(link_id)`; `linking.status(link_id)`.

- [ ] **Step 1: Failing tests** — a known-good signature verifies; a tampered body fails; a >5-min-old timestamp fails; `is_allowed` denies an unlisted user and an empty allowlist; `request_link` → `approve_link` results in `store.binding_for(channel)` returning the project; `deny_link` leaves no binding.
- [ ] **Step 2–4:** Run FAIL → implement (HMAC via `hmac`/`hashlib`) → Run PASS.
- [ ] **Step 5: Commit** `feat(slack): request-signature auth + owner-confirmation linking`.

---

## Task 8: Socket Mode connection, per-thread serialization, cancel, callbacks (`connection.py`)

**Files:**
- Create: `python/errorta_slack/connection.py`
- Test: `python/tests/slack/test_connection.py`

**Interfaces:**
- Consumes: `concierge.run_turn`, `tools.dispatch`, `auth`, `store`, `render`; an **injected** Socket Mode client (`sdk_client`) and `poster` so tests use fakes.
- Produces:
  - `class SlackBridge` with `async def handle_event(envelope: dict) -> None` (acks, dedupes by `event_id`, enqueues per `thread_ts`), `async def _drain(thread_ts)` (FIFO worker; bounded look-ahead cancel scan), `async def handle_interaction(payload: dict) -> None` (verified `block_actions` → `tools.dispatch(..., confirmed_via="block_actions")` → resolve confirmation), `async def start()/stop()`.
  - Reconnect with capped exponential backoff.

- [ ] **Step 1: Failing tests** (async, fake sdk client):
  - two envelopes with the same `event_id` → `run_turn` invoked once (dedupe).
  - three messages on one `thread_ts` arrive fast → processed in FIFO order (assert call order on a spy).
  - a message classified as "stop" queued behind an in-flight launch → the in-flight action's cancel hook fires and the stop message is consumed (look-ahead).
  - `handle_interaction` with an Approve payload calls `tools.dispatch(verb, confirmed_via="block_actions")` and marks the confirmation resolved; a crafted **message-text** "approve" never reaches a C-class dispatch with `confirmed_via="block_actions"`.
- [ ] **Step 2–4:** Run FAIL → implement (asyncio per-`thread_ts` `Queue`/lock) → Run PASS.
- [ ] **Step 5: Commit** `feat(slack): Socket Mode ingress, per-thread FIFO, verified interaction callbacks`.

---

## Task 9: Outbound poller (`outbound.py`)

**Files:**
- Create: `python/errorta_slack/outbound.py`
- Test: `python/tests/slack/test_outbound.py`

**Interfaces:**
- Consumes: `store` (cursor), `team_log`, `attention.list_open`, `publish_ledger`, `render`, an injected `poster`.
- Produces:
  - `poll_once(channel_id, project_id, *, deps, poster) -> list[str]` — diffs current coding-state fingerprint vs `store.get_cursor`; for each new item classifies decision-needed (buttoned, stages a confirmation via `store.stage_confirmation`) vs terminal (FYI); posts via `poster`; advances the cursor; returns posted markers. Exactly-once: if `poster` raises after post but before `advance_cursor`, a re-run must not double-post (guard by writing the marker to a `posted-pending` set first, then advancing).
  - `async def run_loop(interval_s=15)` — timer that calls `poll_once` for each binding.

- [ ] **Step 1: Failing tests** — first `poll_once` posts N items and advances cursor; second call posts 0 (idempotent); an injected `poster` that raises on the 2nd item leaves the cursor such that a re-run posts only the un-posted remainder (no dupes); a blocker (`attention` blocking=True) is rendered as a decision message with a staged confirmation id.
- [ ] **Step 2–4:** Run FAIL → implement → Run PASS.
- [ ] **Step 5: Commit** `feat(slack): outbound cursor-poll over coding-team state`.

---

## Task 10: Routes facade + lifecycle wiring + optionality

**Files:**
- Create: `python/errorta_slack/routes.py`, `python/errorta_app/slack_lifecycle.py`
- Modify: `python/errorta_app/server.py` (lazy import + lifespan boot/stop + `include_router`)
- Test: `python/tests/slack/test_routes.py`, `python/tests/slack/test_optionality.py`

**Interfaces:**
- Produces:
  - `routes.router` (`APIRouter`) — `GET /slack/health`, `GET /slack/status` (enabled + bindings, tokens masked), `POST /slack/enable` / `POST /slack/disable`, `POST /slack/link/approve` (owner action).
  - `slack_lifecycle.sync() -> dict` (start the bridge task iff enabled AND `slack-sdk` importable AND tokens present; else `{"running": False, "reason": ...}`), `slack_lifecycle.stop() -> None`. Both singletons like `mobile_lifecycle`.

- [ ] **Step 1: Failing tests:**
  - `test_optionality`: with `config.is_enabled()` False, `slack_lifecycle.sync()` returns `{"running": False}` and starts nothing; simulate missing `slack-sdk` (monkeypatch import to raise) → `sync()` returns `{"running": False, "reason": "sdk_missing"}`, no exception. Assert `import errorta_app.server` succeeds without `slack-sdk` (grep-style: `errorta_slack` not imported at server module top level — assert via `sys.modules` check after importing server with slack disabled).
  - `test_routes`: `GET /slack/status` on a TestClient returns `enabled False`; enabling requires tokens present (else 400); status masks tokens.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `slack_lifecycle` (lazy `import errorta_slack...` INSIDE the function, never at module top), wire into `server.py` lifespan next to the mobile hooks (`:332` boot, `:432` stop) and `app.include_router(slack_routes.router)` guarded so a missing module doesn't break boot (try/except ImportError logs and continues).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(slack): routes facade + optional lifecycle wiring`.

---

## Task 11: Anti-drift canary + docs

**Files:**
- Create: `python/tests/slack/test_catalog_canary.py`, `docs/SLACK_PM_BRIDGE.md`
- Modify: `README.md` (one line under headless/optional features), `docs/CLI.md` (a short "Slack bridge (optional)" note)

**Interfaces:** none (test + docs).

- [ ] **Step 1: Canary test** — assert the set of verbs the concierge system prompt advertises (parse `build_system_prompt` output, or compare `TOOL_CATALOG` keys) exactly equals the set `tools.dispatch` accepts (introspect the `_TRUST` map / dispatch table). Fails if a verb is added to one and not the other.
- [ ] **Step 2: Run** → PASS (catalog already consistent) — if it fails, reconcile `tools.py`.
- [ ] **Step 3: Docs** — `docs/SLACK_PM_BRIDGE.md`: what it is, that it's optional and off by default, `pip install '.[slack]'`, how to create the Slack app (Socket Mode + `xapp`/`xoxb` scopes) *in prose with placeholders — no real tokens*, the v1/v2 scope boundary (no public URL). README + CLI.md: one line each pointing to it, stressing optional.
- [ ] **Step 4: Commit** `feat(slack): anti-drift canary + docs`.

---

## Final integration gate (after Task 11)

- [ ] Run the full suite: `( cd python && pytest tests/slack -q )` then `( cd python && pytest -q )` — all green.
- [ ] `ruff check python/errorta_slack` clean.
- [ ] Grep guard for secrets/PII in the diff: no `xoxb-`/`xapp-` real tokens, no owner email/home path/hostnames. `git diff main --stat` reviewed.
- [ ] REQUIRED: superpowers:requesting-code-review on the branch before merge.

## Self-Review (author)

- **Spec coverage:** §1 optionality → T1/T10; hygiene → T2/final gate; §3 layout → all; §3.3 concierge → T5; §4 tools + injection ‡ → T4; §4.2 launch scope → T4; §5 behavior (appearance/ambiguity/concurrency/memory/timeout) → T5/T6/T8 (+ timeout-decide handled in T9 confirmation staging + T8 callback; note: the timeout auto-decide *scheduler* is folded into T9's `run_loop` + T3 `pop_pending_older_than`); §6 data flow → T8/T9; §7 auth/secrets → T2/T7; §8 error handling → T4/T5/T8; §9 tests → every task. 
- **Gap noted & closed:** the §5.9 *timeout auto-decide* needs an explicit home — add it to **Task 9** `run_loop`: on each tick, `store.pop_pending_older_than(timeout)` → for each expired confirmation apply its declared `on_timeout` (irreversible classes default decline) via the coding store, post the decision. (Implementer: add a test.)
- **Placeholder scan:** none — every task has concrete signatures + representative tests.
- **Type consistency:** `confirmed_via="block_actions"` marker, `TOOL_CATALOG`, `ToolDeps`, `run_turn`/`TurnResult`, `poll_once` used consistently across T4/T5/T8/T9.
