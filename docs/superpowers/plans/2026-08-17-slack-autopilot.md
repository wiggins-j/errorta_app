# Slack Autopilot + Inbound Decision Rendering (Slice 5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (or executing-plans). Steps use `- [ ]`.

**Goal:** (a) Render the Approve/Decline button when a C-class action is staged from chat (the inbound path never did), and (b) add an owner-enabled `autopilot` mode where the PM approves and executes staged C-class actions itself, through the same verified internal fire path.

**Architecture:** One new config bool `autopilot` (default false). One new connection seam, `_handle_staged_confirmations`, called from `_post_result` after the reply posts: it scans the turn's `tool_results` for `needs_confirmation` entries and, per entry, EITHER posts `render.decision_message` (autopilot off — the manual button) OR claims+fires it via the existing `_fire_confirmed_effect` and posts an audit line (autopilot on). The tool-layer injection guard is untouched — autopilot supplies the same `confirmed_via="block_actions"` a human tap does, only for a structurally-staged action.

## Global Constraints
- **Injection guard stays exactly as-is:** `tools.dispatch`/`studio_tools.dispatch` still require `confirmed_via="block_actions"` for C-verbs; every existing injection test stays green unchanged. Autopilot NEVER dispatches from chat text — only a `needs_confirmation` record already staged in `tool_results` can fire.
- **Default off.** `config.DEFAULT_CONFIG["autopilot"] = False`. Enabling autonomous spend/publish is the owner's explicit config write.
- **Reuse the verified path.** Auto-fire = the same atomic claim (`store.resolve_confirmation(cid,"approved")` → `(record, claimed)`; fire only if `claimed`) + `_fire_confirmed_effect(...)` that `handle_interaction` uses. No second execution route.
- **Audit every autopilot action** with a distinct in-thread line (`🤖 Autopilot …`).
- No `slack_sdk` at module load; optionality holds (`import errorta_app.server` pulls in no `errorta_slack`).
- **Verify in the real venv:** `( cd python && .venv/bin/python -m pytest tests/slack -q )`; `ruff check errorta_slack`. (Subagent shells may lack `slack_sdk` and false-fail — always re-run in `python/.venv`.)

---

## Task 1: `config.autopilot` (default false)

**Files:** Modify `python/errorta_slack/config.py`; Test `python/tests/slack/test_config.py`.

**Produces:** `DEFAULT_CONFIG["autopilot"] = False`; `load()` returns `autopilot` as a normalized bool (truthy→True, missing/garbage→False), mirroring how the other typed keys are coerced.

- [ ] **Step 1: Failing tests** — `load()` with no file → `autopilot is False`; a saved `{"autopilot": True}` → `True`; a saved `{"autopilot": "yes"}` or `{"autopilot": 0}` → normalizes to a bool (True / False respectively — use the same coercion the module already applies to bools; if none exists, `bool(value)`), never raises.
- [ ] **Step 2:** Run FAIL.
- [ ] **Step 3:** Implement: add the key to `DEFAULT_CONFIG`; in `load()` add `"autopilot": _bool(merged.get("autopilot"), default=False)` (add a tiny `_bool` helper next to `_str`, or inline `bool(...)` with a missing-key guard).
- [ ] **Step 4:** Run PASS.
- [ ] **Step 5: Commit** `feat(slack): autopilot config flag (default off)`.

---

## Task 2: Inbound decision button render (autopilot OFF)

**Files:** Modify `python/errorta_slack/connection.py`; Test `python/tests/slack/test_connection.py`.

**Consumes:** `store.get_confirmation`, `render.decision_message`, `studio_tools.TOOL_CATALOG` (for the title text only).

**Produces:**
- A helper `_staged_confirmations(result) -> list[dict]`: return the `tool_results` entries where `status == "needs_confirmation"` and a truthy `confirmation_id`. (Both per-project and studio turn results carry these.)
- A helper `_confirmation_title(record) -> tuple[str, str]`: a short `(title, detail)` from the record's `verb` + `args` (e.g. verb `create_project`, args `{"project_id": "HSQuester"}` → `("Create project HSQuester", "Approve to create the project and its channel.")`; a generic fallback `(f"Confirm {verb}", "Approve to run this action.")` for verbs without a bespoke line). No secrets/PII beyond what the owner typed.
- An `async _handle_staged_confirmations(channel_id, thread_ts, result)` called at the END of `_post_result` (after reactions): for each staged confirmation, when **autopilot is off** (`config.load().get("autopilot")` falsy) look up `store.get_confirmation(cid)` and `post_message(..., blocks=render.decision_message(title, detail, cid))`. (The autopilot-on branch is Task 3; here it does the button in both cases — Task 3 splits it.)

- [ ] **Step 1: Failing tests** —
  - Per-project: a fake `concierge.run_turn` result with `tool_results=[{"status":"needs_confirmation","confirmation_id":"cid1","verb":"start_run"}]` and a staged record in the store → after `handle_event`+`wait_idle`, `poster.messages` includes a block message whose Approve button `value == "cid1"` (assert by scanning blocks for `action_id=="slack_approve"`).
  - Studio: same with `verb="create_project"` in the studio channel → button posted with the cid.
  - Regression: a result with `tool_results=[]` → only the fyi reply, NO `actions` block in any posted message.
- [ ] **Step 2:** Run FAIL.
- [ ] **Step 3:** Implement the three helpers; call `_handle_staged_confirmations` from `_post_result`. Read `store.get_confirmation` for the record (title/detail come from it; if the record is missing — already resolved — skip that cid). Keep it best-effort: a render/post failure for the button must not raise out of the turn (wrap like `_add_reaction_best_effort`? No — a missing button IS a real failure; let it surface via the existing `_post_result` try/except in `_process`). 
- [ ] **Step 4:** Run PASS; full `tests/slack` green.
- [ ] **Step 5: Commit** `fix(slack): render Approve button for chat-staged C-class actions`.

---

## Task 3: Autopilot auto-fire (autopilot ON) + audit line

**Files:** Modify `python/errorta_slack/connection.py`; Test `python/tests/slack/test_connection.py`.

**Consumes:** `store.resolve_confirmation` (atomic claim), `_fire_confirmed_effect`, `_post_effect_error`, `studio_tools.TOOL_CATALOG`.

**Produces:** In `_handle_staged_confirmations`, when **autopilot is on** (`config.load().get("autopilot")` truthy), for each staged confirmation: claim it (`record, claimed = store.resolve_confirmation(cid, "approved")`); if not `claimed`, skip (a concurrent timeout sweep/button won it). If claimed, run `_fire_confirmed_effect(record, channel_id=, thread_ts=, verb=record["verb"], decision="approved", approved=True)` inside try/except; on success post an audit line via a new `_post_autopilot_outcome(verb, channel_id, thread_ts, effect_result)` — `🤖 Autopilot approved & executed *{verb}*.` (studio verbs may fold in `effect_result["status"]` like `_post_studio_outcome` does); on exception `_LOGGER.exception(...)` (metadata only) + `_post_effect_error`. Do **not** also post the decision button in this branch.

- [ ] **Step 1: Failing tests** —
  - Per-project fire: `autopilot=True` (pass via the bridge's `config=` or monkeypatch `config.load`), staged `start_run` record → a spy on `tools.dispatch` is called once with `confirmed_via="block_actions"` and the verb; an audit message containing "Autopilot" is posted; NO `slack_approve` block is posted.
  - Studio fire: staged `create_project` → `studio_tools.dispatch` called with `confirmed_via="block_actions"`; audit posted.
  - Atomic claim: pre-resolve the cid in the store first → autopilot claim returns `claimed=False` → `_fire_confirmed_effect` NOT called, no crash.
  - Error path: `_fire_confirmed_effect` raising → `_post_effect_error` posted, turn does not crash.
  - Injection invariant: `autopilot=True` but a turn with `tool_results=[]` (no staged confirmation) → nothing fired, nothing claimed (autopilot only fires structurally-staged confirmations).
- [ ] **Step 2:** Run FAIL.
- [ ] **Step 3:** Implement the autopilot branch + `_post_autopilot_outcome`. Split the Task-2 helper so off→button, on→claim+fire.
- [ ] **Step 4:** Run PASS; full `tests/slack` green.
- [ ] **Step 5: Commit** `feat(slack): autopilot auto-approves staged C-class actions`.

---

## Task 4: docs + canaries + gate

**Files:** Modify `docs/SLACK_PM_BRIDGE.md`, `docs/SLACK_STUDIO.md`; Test: existing canaries.

- [ ] **Step 1:** `SLACK_PM_BRIDGE.md` — an **Autopilot** section: what it is (PM approves & executes C-class itself), how to enable (`config.save({"autopilot": True})` via the store, default off), that it reuses the verified fire path, and the **security tradeoff** verbatim from spec §4 (no second factor; allowlist + audit line + reversibility as mitigations). Note the button still appears (and works) when autopilot is off — the newly-fixed manual path.
- [ ] **Step 2:** `SLACK_STUDIO.md` — one paragraph: with autopilot on, `create_project`/`archive_project` execute without a tap; default off shows the Approve button.
- [ ] **Step 3:** Full gate in the real venv: `( cd python && .venv/bin/python -m pytest tests/slack -q )` green; `ruff check errorta_slack` clean; `import errorta_app.server` doesn't pull `errorta_slack`; secret/PII grep on the diff. Both catalog canaries green (no catalog change expected).
- [ ] **Step 4: Commit** `docs(slack): autopilot + fixed inbound approve button`.

## Self-Review
- Spec §3.1→T1, §3.2 (button)→T2, §3.3 (auto-fire)→T3, §3.4 coverage→T3 (all C-verbs via `_fire_confirmed_effect` routing), §4 security→T4 docs + T3 injection test, §5 tests→T1-T3, §6 deferred→not built. Types: `autopilot: bool`, `_staged_confirmations(result)->list`, `_handle_staged_confirmations(channel_id, thread_ts, result)`, reuse `_fire_confirmed_effect(record,*,channel_id,thread_ts,verb,decision,approved)`.
