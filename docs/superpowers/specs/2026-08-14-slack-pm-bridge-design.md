# Slack PM Bridge — Design

**Date:** 2026-08-14
**Status:** Design approved (brainstorm). Not yet planned/implemented.
**Module:** `python/errorta_slack/` (new, sibling to `errorta_mobile/`)

---

## 1. Problem & north-star fit

Today you drive the Errorta coding team from the CLI/desktop or a paired phone.
There is no way to talk to the team the way you'd talk to a **project manager you
work with** — casually, asynchronously, from wherever you are:

> **You:** Hey how's the project going? Any problems?
> **PM:** *(checks status)* On track — 3 tasks merged, 1 in review, no blockers.
> **You:** Cool, spin up the approved code so I can test it, send me the link.
> **PM:** *(launches + tunnels)* Running here: https://… (auto-stops in 2h).
> **You:** Couple of bugs — a) …, b) …, c) … — spec them out and add them to the work.
> **PM:** *(writes 3 specs, queues them)* Added as tasks T24–T26. Team will pick them up next cycle.

This serves the north star's **execution-boundary** promise: you decide where each
model runs, and now *where you stand* when you steer the team — at the terminal or
from a Slack thread on your phone. The Slack bridge is a **new surface over the
existing engine**, not new team behavior.

### Optional by construction (hard requirement)

The Slack bridge is **strictly optional**. Errorta's desktop app and CLI must work
fully with the bridge absent, disabled, or its dependency uninstalled:

- Ships as an **optional dependency extra** (`errorta_app[slack]`); the core install
  does not pull the Slack SDK.
- **Enable flag is off by default.** Disabled → no Socket Mode connection, no tokens
  loaded, no routes active, zero behavior change anywhere else.
- The subsystem imports lazily; if `errorta_slack` or its dependency is missing, the
  sidecar boots normally and the feature is simply unavailable (never a hard import
  error in the core boot path).
- Nothing in `errorta_council` / `errorta_cli` / the desktop app may import
  `errorta_slack` at module load — the dependency arrow points one way only.

### Public-repo hygiene (hard requirement)

errorta_app is a **public** repository. No token, key, bot/app token, or personal
data (owner email, home paths, private hostnames) may be committed. Slack app tokens
load at runtime from the existing secret store and are redacted from all bridge logs;
tests use fake/injected seams with placeholder values (`xoxb-…`, `you@example.com`).
Any local runtime state (bindings, cursors) is git-ignored.

### Non-goals (v1)

- Not a replacement for the desktop/CLI PM or the in-run governing PM.
- No multi-tenant Slack marketplace app; single-workspace, self-hosted app.
- No new coding-team capabilities — the bridge only exposes what the engine
  already does.
- No rich editing of specs/config in Slack beyond what the tool surface (§4) lists.

---

## 2. Approved decisions (from brainstorm)

| # | Decision | Choice |
|---|----------|--------|
| Interaction | What the relationship covers | **Two-way governance loop**: inbound tasking + Q&A *and* proactive PM posts (checkpoints, PR-ready, blockers) with in-thread buttons. |
| Trust model | Authority of a chat message | **Hybrid**: reads and launches execute immediately off the message; only the two irreversible actions — **spending on cloud-model calls** and **opening a public PR** — require a one-tap in-thread confirm. Tunable later. |
| Availability | Where the Slack-connected sidecar lives | **Design for both**: default = "my Mac while the sidecar is up" (messages queue while asleep, catch up on wake); documented "always-on host" deployment (e.g. `senditai`) for 24/7. The bridge is deployment-agnostic. |
| Architecture | How Slack connects to the team | **Approach 1**: `errorta_slack` subsystem + a stateless **concierge** front-door agent that maps chat ⇄ a bounded tool surface over the existing engine. |
| Ingress | Transport | **Slack Socket Mode** — the sidecar dials *out* over a WebSocket. No public URL, works behind NAT, identical on laptop or always-on host (this is what makes "both" free). |

---

## 3. Architecture

### 3.1 Module layout

```
errorta_slack/
  config.py       # enable flag; app-level & bot tokens (via secret store); channel↔project bindings
  connection.py   # Socket Mode client: outbound WS, reconnect/backoff, Slack-retry dedupe, graceful stop
  auth.py         # Slack user allowlist ↔ capability-scoped grant (mobile's model)
  linking.py      # owner-confirmation "link this channel to project X" flow (adapted from mobile pairing)
  concierge.py    # front-door agent: message → tool call(s) → reply. Stateless per turn.
  tools.py        # the bounded tool surface (§4) — the ONLY door to the engine
  outbound.py     # subscribes to run events + pending-decisions → posts Block Kit into the bound thread
  render.py       # engine state/events → Block Kit (status cards, buttons, PR/link messages)
  store.py        # durable: channel bindings, thread↔run map, processed-event cursor (atomic-write, mobile style)
  routes.py       # tiny FastAPI facade: enable/disable, link status, health (NOT the Slack ingress)
```

### 3.2 Three hard boundaries

1. **Ingress is Socket Mode inside the sidecar** (`connection.py`), never a public
   webhook. The sidecar owns the connection and starts/stops it with its own
   lifecycle (like `errorta_mobile`). "Always-on host" therefore = "run the sidecar
   on an always-on host" — no separate service to operate.
2. **`tools.py` is the only door to the engine.** The concierge cannot call
   `errorta_council` / `errorta_policy` / `runtime_process` / `errorta_tunnels`
   ad hoc — it gets a fixed, audited verb list (§4). Same **grant-or-refuse**
   discipline the coding roles already enforce (`turn_controller._ROLE_TOOLS`):
   an unknown/unlisted verb is rejected fail-closed and named back to the model.
3. **The concierge is a stateless front-door, distinct from the in-run governing
   PM.** It speaks *for* the team and reads/writes the control plane; it never
   enters the deterministic run loop or consumes its reasoning budget.

### 3.3 The concierge turn

`concierge.py` runs one LLM turn per inbound Slack message:

- **System prompt** = `pm_reference.build_pm_reference_context()` (the PM's manual)
  + a live-state snapshot for the bound project (models installed, current config,
  run status) + the tool catalog from `tools.py` + the Slack-etiquette contract
  (brevity, thread discipline, the hybrid-trust rules).
- **Input** = the user's message + a short rolling transcript of that Slack thread
  (bounded window; the thread *is* the memory — nothing is invented across threads).
- **Output** = a strict-ish JSON envelope: `reply` (Slack text) + zero-or-more
  `tool_calls`. Tool calls execute through `tools.py`; results fold back into a
  follow-up turn if the model needs them to compose its reply (bounded hop count).
- **Model** = user-selected per binding (default: a fast interactive model — chat
  latency matters). Reuses the council member-caller injection seam, so the whole
  concierge is unit-testable without egress.

---

## 4. The tool surface (`tools.py`)

The complete v1 verb list. Each verb names the existing engine seam it drives and
its trust class (**R** = executes immediately; **C** = requires in-thread confirm).

| Verb | Trust | Drives | Purpose |
|------|:---:|--------|---------|
| `list_projects` | R | `room_store` / coding project store | "what are you working on?" |
| `switch_project` | R | binding in `store.py` | rebind this channel/thread to another project |
| `project_status` | R | `coding/project_status.py`, run stream | "how's it going? any problems?" → status card |
| `recent_activity` | R | `team_log` / event stream | "what happened overnight?" |
| `launch_runtime` | R | `runtime_process` (F101) **+** `errorta_tunnels` (F089) | spin up approved build on a loopback port, tunnel it, return a shareable URL with a TTL |
| `stop_runtime` | R | `runtime_process` | tear the preview down |
| `spec_bugs` | R | Wizard/charter intake → task queue (`ledger`) | turn a free-text bug list into N specs, queue as tasks |
| `answer_question` | R | (no side effect) | pure Q&A grounded in status/context |
| `resolve_decision` | R\* | `errorta_policy` pending-decision store (F041) | Accept/Decline an *already-surfaced* checkpoint/PM-Changes review (the button path) |
| `spend_cloud` | **C** | model gateway / budget | any action that will spend on **cloud** model calls |
| `publish_pr` | **C** | `coding/publish_github.py` | open/update a **public** PR |

\* `resolve_decision` executes immediately because the human tap on an Approve/Decline
button *is* the confirmation — the decision was already surfaced with full context.

**Trust enforcement lives in `tools.py`, not in the prompt.** A **C**-class verb
never executes on the first call: the tool returns a `needs_confirmation` result
carrying a durable pending-decision id; `outbound.py` renders it as an Approve/Decline
message; the button callback re-invokes the verb with the confirming id. This reuses
F041 end-to-end, so a Slack confirmation is the same audited artifact as a
desktop one — no parallel approval path.

### 4.1 Worked scenario (the three messages)

1. **"How's it going? Any problems?"** → concierge emits `project_status`
   (R, executes) → `render.py` → status card: merged/in-review/blocked counts,
   any blockers. One turn, no confirm.
2. **"Spin up the approved code, send me the link."** → `launch_runtime` (R):
   `runtime_process` starts the approved build on a loopback port behind the F039
   sandbox; `errorta_tunnels` exposes it; the concierge replies with the URL and
   its auto-stop TTL. If nothing is in the "approved" state, the tool returns an
   empty-state result and the PM says so (grounded-or-refuse) rather than launching
   something stale.
3. **"Bugs: a…, b…, c… — spec them and add them to the work."** → `spec_bugs` (R):
   three charter-style specs are written and queued as tasks via the same intake
   path the Wizard uses; the PM replies with the task ids. *(If a queued bug would
   trigger cloud spend or a public PR, that surfaces later as a **C** confirm at the
   point of spend/publish — not at queue time.)*

---

## 5. Behavioral contract (how the PM acts)

Grilled out with the owner. This is the conduct spec — the concierge's
Slack-etiquette system prompt (§3.3) encodes it; where a rule is mechanical it is
enforced in code, not left to the model.

### 5.1 Addressing & channel model
- One **dedicated channel per binding** (Q1). The PM reads the channel and posts its
  own proactive pings there. In a dedicated bound channel it acts on the owner's
  messages without needing an @-mention; only **allowlisted** users (§6) can drive it.
- **Replies are only ever expected on a proactive ping the PM flags critical.** FYI
  posts want no answer.

### 5.2 Proactive posting (Q7)
- Push exactly two classes: **(a) decisions it needs from you** (critical — wants a
  reply) and **(b) terminal states** (run done/failed, PR ready — FYI).
- **Progress is pull-only** — you ask "how's it going" to get it. No unprompted
  drip. No quiet hours in v1 (mute in Slack if noisy). A run-complete summary
  batches the progress so you're not drip-fed.

### 5.3 Message appearance (Q8, Q10)
- **Critical decision** = unmistakable: a 🔴-labeled Block Kit header ("DECISION
  NEEDED"), an @-mention (so it pings your phone), and Approve/Decline buttons — one
  tap, no typing.
- **FYI** = plain threaded post, no mention, no buttons.
- **Reactions carry ack state**: 👀 on receipt of your message, ✅ when an action
  lands and the PM was confident, **🤔 when the PM acted on a guess** (§5.4) so a
  wrong assumption is scannable at a glance.

### 5.4 Ambiguity: best-guess-and-act (Q3)
- Under-specified message → the PM **acts on its best interpretation and states the
  assumption as the first clause of its reply**, tagging that message 🤔
  ("Assumed the *reviewer-approved* build — say so if you meant the other").
- **Exception:** if the ambiguity is on a **C**-class action (cloud spend / public
  PR), it asks instead of guessing.

### 5.5 Concurrency (Q4)
- **Serialize per thread**: finish the in-flight action, then process queued messages
  in order, acking receipt ("on it — queued your other two").
- A later message that plainly contradicts an in-flight one ("stop, don't launch") is
  honored as a **cancel** of that specific action, not the whole run.

### 5.6 Persona (Q5)
- **Terse, friendly senior PM.** Short, factual, leads with the answer, no filler.
  Reactions (§5.3) do the lightweight acking so you never wait on prose to know it
  heard you.

### 5.7 Memory (Q6) — Slack thread is the source of truth
- **Conversation memory = the Slack thread itself.** Every concierge turn reads the
  thread's messages back from Slack — the PM's own posts *and* your replies — so your
  reply always arrives with the original PM message it answers, for free, with no
  separate transcript that could drift.
- **The durable store owns only what Slack can't tell us**: channel bindings, the
  **outbound cursor** (which run-events it has already posted — idempotency), and an
  **explicit-preferences** record. Preferences are recorded **only when you state
  one** ("always confirm before cloud spend") — never silently inferred.

### 5.8 Preference precedence (Q9)
- **North Star > committed project config > stated Slack preference.** A Slack
  preference *proposes* a config change (surfaced as a PM Change), it does not
  silently override committed config, and it can never cross the North Star.

### 5.9 Critical blocks: nudge, then timeout-decide (Q11, Q12, Q13)
- A critical block **@-mentions you with buttons** and waits. Only the **blocked task
  (and its dependents) holds — independent tasks keep moving** under the parallelism
  limit. A genuinely run-wide decision halts everything (that is its nature).
- If you don't answer: **one nudge re-ping** after an interval, then on a
  **timeout the PM decides for you and posts the decision it made + why**.
- **Timeout applies to every block class, including the two irreversible ones**
  (owner's explicit call — full autonomy). Two safeguards make this survivable and
  are non-negotiable in code: (1) the auto-decision is **always the conservative
  option** — for cloud-spend that means *don't spend* / stay local, for public-PR
  that means *don't publish* — so an unattended timeout never spends money or
  publishes unless that is itself the safe choice; (2) it is written through the
  **F041 pending-decision store**, so an auto-decision is the same audited artifact
  as a human tap.
- **Timeout default 30 min, configurable per binding.**

---

## 6. Data flow & threading

### 5.1 Inbound

```
Slack msg ─▶ connection.py (Socket Mode; ack + dedupe Slack retries)
          ─▶ auth.py (allowlisted user? capability?) ─▶ store.py (resolve binding + thread→run)
          ─▶ concierge.py (LLM turn) ─▶ tools.py (execute R / stage C) ─▶ render.py ─▶ reply in-thread
```

### 5.2 Outbound (proactive)

```
run event stream / F041 pending-decision store
          ─▶ outbound.py (poll from a durable per-binding cursor in store.py)
          ─▶ filter to postable events (checkpoint, CALLOUT_APPROVAL_REQUIRED,
             BUDGET_BLOCKED, PR-ready, run-complete, run-failed)
          ─▶ render.py (Block Kit + buttons) ─▶ post into the bound thread
```

### 5.3 Threading model

- **One binding = one project ↔ one Slack channel** (`store.py`). A DM to the bot
  is treated as a private channel binding.
- **A run gets a thread.** Proactive posts and the user's replies about that run
  stay in its thread → the concierge's rolling-window memory is the thread itself.
- The **processed-event cursor is durable and per-binding**: a sidecar restart (or
  the laptop waking) resumes outbound posting exactly once — no gaps, no dupes.
  This is the mechanism behind "messages queue while asleep, catch up on wake."

---

## 7. Auth, pairing & security

Reuses `errorta_mobile`'s hard-won discipline; does not reinvent it.

- **Enable is off by default** (`config.py`), like the mobile connector. Disabled
  bridge = no Socket Mode connection, no tokens loaded.
- **Slack app tokens** (app-level token for Socket Mode + bot token) live in the
  existing secret store, never in config files or logs; redacted from all bridge
  logs (mirror `runtime_process` redaction).
- **Channel linking is owner-confirmation** (`linking.py`, adapted from the F065
  pairing state machine): a Slack user requests to link a channel to a project;
  the **desktop/CLI owner approves**; only then is a capability-scoped grant minted.
  The secret never exists before the human consents.
- **User allowlist + capabilities** (`auth.py`): only allowlisted Slack user ids may
  drive the PM; **C**-class verbs can be gated to a narrower set. An unlisted user's
  message is ignored with an audit line — never a silent action.
- **Trust classes are enforced in `tools.py`** (§4), backed by F041 — the confirm
  path is the same audited decision artifact as the desktop path.
- **Egress boundary preserved**: `errorta_slack` reaches subprocess/tunnel/HTTP only
  through the existing `errorta_tools` / `errorta_tunnels` seams; it adds exactly one
  new outbound endpoint (Slack) via the Socket Mode client, and no inbound port.
- **Instruction-source boundary**: message *content* is data, not commands. The
  concierge treats Slack text as a user request against its fixed tool surface; it
  cannot be talked into a verb it wasn't granted (fail-closed, named back).

---

## 8. Error handling

- **Socket disconnect** → `connection.py` reconnects with capped backoff; inbound
  events Slack redelivers are deduped by event id (`store.py`); the outbound cursor
  guarantees at-least-once → exactly-once posting.
- **Concierge produces malformed JSON / an unknown verb** → rejected fail-closed,
  the allowed catalog is named back into a corrective follow-up turn (SPEC-17
  pattern); if it still fails, the PM posts a plain "I couldn't act on that —
  here's what I can do" with the verb list. Never a silent drop.
- **A tool call fails** (launch can't bind, tunnel down, queue write conflict) →
  the failure is surfaced verbatim-but-redacted in-thread ("couldn't start the
  preview: port in use — want me to retry?"), never swallowed.
- **`launch_runtime` with nothing approved** → empty-state reply, not a stale launch.
- **Model/budget unavailable for the concierge turn** → the bridge degrades to a
  deterministic status card for `project_status`/`recent_activity` (no model needed)
  and tells the user the conversational PM is offline.
- **Binding missing / project deleted** → the bot asks the user to (re)link rather
  than acting against a dangling project.

---

## 9. Testing

Everything is unit-testable without egress — the Slack client, the model caller,
and the tunnel are all injected seams.

- **Concierge mapping tests** — table of (message, expected tool_calls) covering the
  three scenario messages + ambiguous/empty-state/hostile inputs. Model caller is a
  fake returning canned envelopes.
- **Trust-class tests** — `spend_cloud` / `publish_pr` never execute on first call;
  produce a pending-decision; execute only when re-invoked with the confirming id.
  Cross-checked against the F041 store so the Slack path and desktop path converge.
- **Outbound cursor tests** — restart mid-stream posts each event exactly once; a
  crash between "posted" and "cursor advanced" does not double-post.
- **Auth/linking tests** — unlisted user ignored+audited; link requires owner
  approval; token minted only at approval; redaction holds across logs.
- **Connection tests** — Slack retry of the same event id is deduped; reconnect
  backoff is bounded.
- **Anti-drift canary** — like the PM_REFERENCE canary: a test asserts the tool
  catalog the concierge is shown matches the verbs `tools.py` actually implements,
  so the prompt can't advertise a capability the code lacks.
- **`launch_runtime` empty-state test** — nothing approved → no process started.
- **Critical-block timeout tests** (§5.9) — a critical block with no reply nudges
  once, then on timeout auto-decides; the auto-decision is the **conservative**
  option (cloud-spend block → *don't spend*; public-PR block → *don't publish*); the
  chosen decision is written through F041 and posted back. A human tap before timeout
  cancels the pending nudge/auto-decide.
- **Block scoping test** (§5.9) — a per-task critical block holds only that task and
  its dependents; independent tasks keep progressing; a run-wide decision halts all.
- **Ambiguity-signal test** (§5.4) — an under-specified message is acted on with the
  assumption stated first and the message tagged 🤔; a **C**-class ambiguity asks
  instead of guessing.
- **Thread-as-memory test** (§5.7) — a concierge turn reconstructs context from the
  Slack thread (its own prior post + the user reply), with no separate transcript;
  the durable store holds only bindings, outbound cursor, and explicit preferences.

---

## 10. Open questions (deferred, not blocking)

- Should `spec_bugs` run the bug list through the Wizard's runnable-by-construction
  check, or queue lighter "bug" tasks? (Lean: lightweight bug tasks; full charter
  intake is overkill for a bug.)
- Multiple projects in one channel via threads, vs strict one-channel-one-project?
  (v1: strict; revisit if it chafes.)
- Slash-command fast-paths (`/errorta status`) as a cheap non-LLM shortcut alongside
  chat? (Nice-to-have; out of v1 scope.)
