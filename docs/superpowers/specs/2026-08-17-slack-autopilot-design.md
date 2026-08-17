# Slack Autopilot + Inbound Decision Rendering — Design (Slice 5)

**Date:** 2026-08-17
**Status:** Design (autonomous continuation). Not yet implemented.
**Depends on:** studio manager (S1) + run control (S2) + spin-down/reconfigure (S3), merged.
**Modules:** `errorta_slack/{config,connection,render}.py` (+ docs).

---

## 1. Problem

Two problems, discovered together during the live studio test.

**(a) The inbound decision button is missing (latent bug).** When the owner
chats a **C**-class action ("create the project", "start building", "publish
the PR"), the concierge stages a confirmation and `tools.dispatch` returns
`{"status":"needs_confirmation","confirmation_id":…}` in the turn's
`tool_results`. But `connection._post_result` renders **only**
`render.fyi_message(reply)` — it never inspects `tool_results` for a staged
confirmation and never posts `render.decision_message`. The Approve/Decline
buttons exist **only** on the outbound proactive poller (`outbound.py:273`).
Net effect: a C-class action requested *in chat* stages a confirmation with
**no button anywhere to approve it**. The PM (correctly, per its grounding)
says "press Approve", but there is nothing to press, and typing "approve"
is just text (the injection guard ignores it). The manual approve path is
effectively dead for inbound requests.

**(b) The owner does not want to tap a button at all.** The product intent is
"chat a PM who *does the thing*." Requiring a human tap for every C-class
action defeats that. The owner wants the PM to approve and execute itself —
"the PM that Slack talks to should be able to do anything in errorta that
it's directed to do."

## 2. Grounded facts

- **Trust model (unchanged at the tool layer).** `tools.dispatch` /
  `studio_tools.dispatch` run a **C**-verb only when
  `confirmed_via == "block_actions"`; with `confirmed_via=None` (what every
  concierge turn passes) they `stage_confirmation` and return
  `needs_confirmation` + `confirmation_id`. This is the injection guard and it
  **stays exactly as is** — autopilot never relaxes it.
- **The verified fire path already exists.** `connection.handle_interaction`
  claims a staged confirmation atomically (`store.resolve_confirmation` →
  `(record, claimed)`), then `_fire_confirmed_effect(record, …)` dispatches the
  verb with `confirmed_via="block_actions"`, routing studio verbs
  (`verb in studio_tools.TOOL_CATALOG`) to `studio_tools.dispatch` and the rest
  to `tools.dispatch`, then posts an outcome. Autopilot reuses this path
  verbatim — it does **not** invent a second execution route.
- **The staged id is already in hand.** `concierge.run_turn` /
  `studio_concierge.run_turn` return `tool_results` containing the
  `needs_confirmation` record(s) with `confirmation_id`, so both the button
  (a) and the auto-fire (b) can key off the same value already present in the
  result — no new plumbing to surface it.
- **Config is a persisted JSON dict** (`config.load()/save`), already carrying
  `studio_model_route`, `studio_default_team`, allowlist ids. A new boolean
  slots in the same way, with a safe default.

## 3. Design

### 3.1 Config — `autopilot` (default **false**)

Add `"autopilot": False` to `config.DEFAULT_CONFIG`, normalized as a bool in
`config.load()` like the other typed keys. **Default false** preserves today's
behavior for everyone and keeps the feature opt-in — enabling autonomous
spend/publish is the owner's own deliberate act (a config write), never a
side effect of installing the bridge.

### 3.2 Inbound decision rendering (fixes 1a) — autopilot **off**

`_post_result` (the shared inbound render, used by both `_process` and
`_process_studio`) gains a step: after posting the reply, scan
`result["tool_results"]` for any entry with
`status == "needs_confirmation"` and a `confirmation_id`. For each, look up the
staged record (`store.get_confirmation`) and post a
`render.decision_message(title, detail, confirmation_id)` so the owner has a
real Approve/Decline button in-thread. Title/detail derive from the record's
verb + safe args (a short human line — e.g. "Create project *HSQuester*",
"Start the coding run"). This makes the **manual** path work at last.

### 3.3 Autopilot auto-fire (delivers 1b) — autopilot **on**

When `config.load()["autopilot"]` is true, instead of posting a button for a
freshly staged confirmation, the bridge **immediately approves it through the
existing verified path**: for each `needs_confirmation` in `tool_results`,
claim it (`store.resolve_confirmation(cid, "approved")` → `(record, claimed)`;
only the winner fires — same atomic claim the button and the timeout sweep
already share) and run `_fire_confirmed_effect(record, …)`, then post a
distinct audit line — `🤖 Autopilot approved & executed *{verb}*.` (or a
`_post_effect_error` on failure). Because the fire path dispatches with
`confirmed_via="block_actions"`, **the tool-layer injection guard is
satisfied by the same marker a human tap produces** — autopilot supplies the
approval, it does not bypass the gate. Execution still flows from a
**structured staged action** (the concierge's tool call with concrete,
audited args), never from regex over chat text.

### 3.4 What autopilot covers

**All** C-class verbs — per-project (`start_run`, `spend_cloud`, `publish_pr`)
and studio (`create_project`, `archive_project`) — honoring "do anything it's
directed to do." One global flag; no per-verb granularity in v1 (trivial to
add later by consulting the verb before firing).

## 4. Trust / security (read this)

- **The injection guard is intact.** `dispatch` still refuses a C-verb without
  `confirmed_via="block_actions"`; every existing injection test stays green
  unchanged. Autopilot is a *connection-layer policy* that auto-supplies the
  verified approval for a **just-staged, structured** action — it is not
  "execute from chat text."
- **The allowlist still gates all input.** Only `allowed_team_ids` /
  `allowed_user_ids` messages ever reach a turn; non-owners can't drive the PM
  whether autopilot is on or off.
- **Residual risk (documented, owner-accepted).** With autopilot **on** there
  is no second human factor, so content the owner *pastes* that the model
  treats as an instruction could drive a C-class action. Mitigations kept:
  (1) default **off** — opt-in only; (2) every autopilot action posts a loud
  in-thread audit line, so nothing happens silently; (3) all of it is
  reversible from chat (`stop_run`, `archive_project`). The doc states this
  plainly so enabling it is an informed choice.
- **Optionality unchanged.** No new import at module load; `autopilot` is read
  from the existing config. `import errorta_app.server` still pulls in no
  `errorta_slack`.

## 5. Testing (egress-free)

- **Config:** `autopilot` defaults false; round-trips true; a garbage value
  normalizes to false.
- **Inbound button (off):** a turn whose `tool_results` carries a
  `needs_confirmation` + `confirmation_id` → `_post_result` posts a
  `decision_message` block whose button `value` == the `confirmation_id`
  (both per-project and studio). A turn with no staged confirmation → only an
  `fyi_message`, no buttons (regression guard).
- **Autopilot auto-fire (on):** same staged turn, `autopilot=True` →
  `_fire_confirmed_effect` runs the verb via the correct dispatch
  (`tools` vs `studio_tools` by catalog membership) with
  `confirmed_via="block_actions"`; an audit line is posted; **no** decision
  button is posted. The claim is atomic (a second fire of the same cid is a
  no-op). A dispatch error → `_post_effect_error`, no crash.
- **Injection invariant unchanged:** with `autopilot=False`, chat text
  containing "approve" still stages/does-nothing (existing tests); with
  `autopilot=True`, a turn that stages **no** confirmation fires nothing
  (autopilot only ever fires a structurally-staged confirmation, never
  free text).
- **Canaries** (`test_catalog_canary`, `test_studio_catalog_canary`) stay
  green — no catalog/verb changes.

## 6. Deferred

- Per-verb / per-channel autopilot granularity (e.g. autopilot everything
  except `publish_pr`). One global flag in v1.
- A "dry-run/confirm-once-then-trust" middle mode. Binary on/off in v1.
- Undo of an autopilot action beyond the existing `stop_run`/`archive_project`.
