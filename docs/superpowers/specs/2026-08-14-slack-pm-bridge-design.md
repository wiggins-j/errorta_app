# Slack PM Bridge — Design

**Date:** 2026-08-14
**Status:** Design approved (brainstorm + behavioral grill), **revised after an
adversarial architecture review** that corrected which engine seams exist (the
coding team uses `team_log`/`attention`/`pm_changes`/`publish_ledger`, *not* the
council event stream or F041), closed a prompt-injection hole (§4 ‡), and deferred
the phone-openable public URL to v2 (§4.2/§10). Not yet planned/implemented.
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
load at runtime from a Slack-scoped `0600` token store (§7.1) and are redacted from all bridge logs;
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
| Interaction | What the relationship covers | **Two-way governance loop**: inbound tasking + Q&A *and* proactive PM posts (decisions-needed, PR-ready, blockers) with in-thread buttons. |
| Trust model | Authority of a chat message | **Hybrid**: reads and launches execute immediately off the message; only the two irreversible actions — **spending on cloud-model calls** and **opening a public PR** — require a one-tap in-thread confirm. Tunable later. |
| Availability | Where the Slack-connected sidecar lives | **Design for both**: default = "my Mac while the sidecar is up" (messages queue while asleep, catch up on wake); documented "always-on host" deployment (e.g. `senditai`) for 24/7. The bridge is deployment-agnostic. |
| Architecture | How Slack connects to the team | **Approach 1**: `errorta_slack` subsystem + a stateless **concierge** front-door agent that maps chat ⇄ a bounded tool surface over the existing engine. |
| Ingress | Transport | **Slack Socket Mode** — the sidecar dials *out* over a WebSocket. No public URL, works behind NAT, identical on laptop or always-on host (this is what makes "both" free). |

---

## 3. Architecture

### 3.1 Module layout

```
errorta_slack/
  config.py       # enable flag (off by default); channel↔project bindings; window/timeout knobs
  secrets.py      # Slack token store: 0600 JSON under ${ERRORTA_HOME}/slack/ (§7.1), modeled on provider_keys.py
  connection.py   # Socket Mode client: outbound WS, reconnect/backoff, event_id dedupe, ≤3s ack, graceful stop
  auth.py         # Slack team/user allowlist + request-signature verify ↔ capability grant
  linking.py      # owner-confirmation "link this channel to project X" (F065 state machine; Slack-signing anchor)
  concierge.py    # front-door agent: message → tool call(s) → reply. Stateless per turn. Injected model-caller.
  tools.py        # the bounded tool surface (§4) — the ONLY door to the engine; grant-or-refuse fail-closed
  outbound.py     # cursor-polls the coding-team state (team_log/attention/publish_ledger) → posts Block Kit
  render.py       # coding-team state → Block Kit (status cards, buttons, PR/link messages)
  store.py        # durable atomic-write: bindings, per-thread queue, outbound cursor, confirmation records, prefs
  routes.py       # tiny FastAPI facade: enable/disable, link status, health (NOT the Slack ingress)
```
Dependencies point one way: `errorta_slack` imports the engine; nothing in the core
boot path imports `errorta_slack` (§1 optionality). The Slack SDK is an optional extra.

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

The complete v1 verb list. Each verb names the **real coding-team seam** it drives
(verified against the code in the architecture review — the coding team does *not*
use the council event stream or the F041 policy store; those are a different engine)
and its trust class (**R** = executes immediately; **C** = requires a verified
in-thread button confirm).

| Verb | Trust | Drives (real seam) | Purpose |
|------|:---:|--------|---------|
| `list_projects` | R | coding project store (`coding/ledger.py`) | "what are you working on?" |
| `switch_project` | R | binding in `store.py` | rebind this channel to another project |
| `project_status` | R | `coding/team_log.build_team_log(store)` + `coding/attention.list_open(project_id)` (model-free) | "how's it going? any problems?" → status card |
| `recent_activity` | R | `team_log` decision/event log tail | "what happened overnight?" |
| `launch_runtime` | R | `runtime_process` (F101) — **loopback only** | start the current worktree's `RuntimeProfile`; return the **local/LAN URL** (see §4.2) |
| `stop_runtime` | R | `runtime_process` | tear the preview down |
| `queue_bugs` | R | `coding/ledger.add_task(...)` (gate-free append) | turn a free-text bug list into N queued `todo` tasks |
| `answer_question` | R | (no side effect) | pure Q&A grounded in the status context already fetched |
| `resolve_decision` | **R‡** | `pm_changes.accept/decline` + governance + `publish_gate` (the coding approval stores) | Accept/Decline an *already-surfaced* PM-Change / gate decision |
| `spend_cloud` | **C** | model gateway / budget | any action that will spend on **cloud** model calls |
| `publish_pr` | **C** | `coding/publish_github.py` + `publish_ledger` | open/update a **public** PR |

**‡ `resolve_decision` and every C-class confirm execute ONLY from a verified Slack
`block_actions` interaction payload — never from concierge text output** (review C5).
The concierge may *propose* a decision; it may never *self-confirm* one. A confirming
id minted from parsing chat text is rejected. This closes the prompt-injection path
where untrusted pasted text ("approve the pending request") could resolve a
cloud-spend or public-PR decision without a human tap.

**Trust enforcement lives in `tools.py`, not in the prompt.** A **C**-class verb
never executes on the first call: the tool returns a `needs_confirmation` result
carrying a durable **bridge-owned** confirmation record (§7.1 — *not* F041, which is
council-scoped and requires a `run_id` the chat path may not have); `outbound.py`
renders it as a buttoned message; the verified button callback re-invokes the verb
with the confirming id. Coding-context approvals resolve through the coding stores
(`pm_changes` / governance / publish gate), so a Slack confirm is the same audited
artifact as the desktop one.

### 4.1 Worked scenario (the three messages)

1. **"How's it going? Any problems?"** → `project_status` (R): reads `team_log` +
   `attention.list_open` (no model needed) → status card: tasks done/in-review,
   open blockers. One turn, no confirm.
2. **"Spin up the code, send me the link."** → `launch_runtime` (R): `runtime_process`
   starts the current worktree's configured `RuntimeProfile` on a **loopback** port
   behind the F039 sandbox and returns `allocated_ports`. v1 replies with the **local
   (or, on an always-on host, LAN) URL** plus its auto-stop TTL — and says so plainly.
   A phone-openable *public* URL is **v2** (§4.2 / §10): it needs an authenticated
   public ingress that does not exist today. If no `RuntimeProfile` is configured, the
   tool returns an empty-state result and the PM says so (grounded-or-refuse).
3. **"Bugs: a…, b…, c… — add them to the work."** → `queue_bugs` (R): three `todo`
   tasks are appended via `ledger.add_task` (requires the project to already exist);
   the PM replies with the task ids and notes they run on the next team cycle.
   *(Turning a bug into a governed spec artifact + model call is heavier and out of
   v1 — see §10.)*

### 4.2 `launch_runtime` link scope (review C1)

`runtime_process.start()` binds **loopback only** by design, and `errorta_tunnels`
is an `ssh -L` **local** forward — it cannot expose a local dev server to a phone.
So:

- **v1:** `launch_runtime` returns the loopback URL (default host) or the LAN URL
  when the sidecar runs on an always-on host reachable on the user's network. The PM
  states which it is. No public URL, no new ingress.
- **v2 (deferred):** an authenticated public ingress (Tailscale Funnel / cloudflared)
  + a TTL auto-stop supervisor. Tracked in §10; not built in v1.

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

### 5.5 Concurrency (Q4) — mechanism (review D3)
- Socket Mode delivers events concurrently over one WS and demands a ≤3s ack, so the
  bridge **acks immediately, then enqueues** onto a per-`thread_ts` async queue keyed
  in `store.py`; inbound events are **deduped by Slack `event_id`** before enqueue.
- **Serialize per thread**: one worker per thread drains its queue in FIFO order,
  acking receipt ("on it — queued your other two").
- **Cancel reconciliation**: before starting the *next* queued item, the worker does a
  bounded look-ahead scan of its own queue for an explicit **cancel token** (a message
  the concierge classifies as "stop/cancel the in-flight action"); if found, the
  in-flight action is cancelled and the cancel message consumed. This is the *only*
  peek-ahead — it does not reorder normal messages, preserving FIFO for everything else.

### 5.6 Persona (Q5)
- **Terse, friendly senior PM.** Short, factual, leads with the answer, no filler.
  Reactions (§5.3) do the lightweight acking so you never wait on prose to know it
  heard you.

### 5.7 Memory (Q6) — Slack thread is the source of truth
- **Conversation memory = the Slack thread itself.** Every concierge turn reads the
  thread's messages back from Slack (`conversations.replies`) — the PM's own posts
  *and* your replies — so there is no separate transcript that could drift.
- **Concrete window rule (review D2):** the turn context = **the last N messages of
  the thread** (N a configurable budget, default 20) **plus always the parent message
  referenced by `thread_ts`** if it falls outside that window, capped at a token
  budget with the oldest middle messages truncated. This guarantees a reply always
  carries the PM message it answers without re-tokenizing an unbounded thread.
  `conversations.replies` paginates at ~100/page under per-method rate limits, so the
  fetch is a single bounded page in the common case.
- **The durable store owns only what Slack can't tell us**: channel bindings, the
  **outbound cursor** (which coding-state items it has already posted — idempotency),
  and an **explicit-preferences** record. Preferences are recorded **only when you
  state one** ("always confirm before cloud spend") — never silently inferred.

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
  (owner's explicit call — full autonomy). Safeguards, non-negotiable in code:
  1. **The two irreversible classes have a defined conservative default**: cloud-spend
     → *don't spend* / stay local; public-PR → *don't publish*. An unattended timeout
     never spends money or publishes.
  2. **Every other (reversible) block declares its own timeout default** at the point
     it is raised — because there is no universal "conservative" answer (review D1: an
     `approve` may apply a state-write that *lowers a safety limit*, so blanket
     approve-to-proceed is unsafe, and blanket reject can strand a run). The raiser
     tags each block with `on_timeout: approve|decline` reflecting the safe choice
     *for that block*; the bridge honors that tag, never a global rule.
  3. The auto-decision is written through the **coding approval stores it belongs to**
     (`pm_changes` / governance / publish gate — *not* the council F041 store, which
     is run-scoped; review C3), so it is the same audited artifact as a human tap.
- **Timeout default 30 min, configurable per binding.**

---

## 6. Data flow & threading

### 6.1 Inbound

```
Slack msg ─▶ connection.py (Socket Mode; ack ≤3s + dedupe by event_id)
          ─▶ auth.py (allowlisted user? capability?) ─▶ store.py (resolve binding, enqueue per thread_ts)
          ─▶ concierge.py (LLM turn) ─▶ tools.py (execute R / stage C) ─▶ render.py ─▶ reply in-thread
```
C-class / decision confirms re-enter through the **verified `block_actions` callback**
(§4 ‡), a separate path from message text — never from concierge output.

### 6.2 Outbound (proactive) — over the real coding-team state (review C2/C4)

```
coding-team state (per bound project_id):
   team_log.build_team_log(store)   — decision/event log (model-free)
   attention.list_open(project_id)  — open blockers → critical decisions
   publish_ledger / publish_gate    — PR-ready (ledger choice="pr_opened")
   pm_changes / governance          — approvals needing a human
          ─▶ outbound.py (poll on a timer; diff against a durable per-binding cursor)
          ─▶ classify: decision-needed (critical, buttons) vs terminal (FYI)
          ─▶ render.py (Block Kit) ─▶ post into the bound thread
```
There is **no push/subscribe** in the engine — the coding runner emits no council
events and `RunStore` has no tail. Outbound is **cursor-polling** that diffs the
coding-team state and posts the delta. The cursor is a bridge-owned durable marker
(§7.1); the poll re-reads whole logs, acceptable at coding-team cadence but noted as
the bridge's own new plumbing, not a reused stream.

### 6.3 Threading model

- **One binding = one project ↔ one Slack channel** (`store.py`).
- **Each surfaced decision / run gets a thread.** Proactive posts and your replies
  stay in that thread → the §5.7 window is over that thread.
- The **cursor is durable and per-binding**: a sidecar restart (or the laptop waking)
  resumes outbound posting exactly once — no gaps, no dupes. This is the mechanism
  behind "messages queue while asleep, catch up on wake."

---

## 7. Auth, pairing & security

Borrows `errorta_mobile`'s owner-confirmation *pattern*; the crypto anchor differs
(review C6) and is corrected below.

- **Enable is off by default** (`config.py`). Disabled bridge = no Socket Mode
  connection, no tokens loaded, no routes, zero behavior change elsewhere (§1
  optionality). Missing Slack SDK → feature simply unavailable, never a boot error.
- **Slack token storage is new work** (review C6 — no unified secret store exists
  today). §7.1 specifies it: a Slack-scoped `0600` token store modeled on
  `errorta_app/provider_keys.py`, holding the app-level (`xapp-…`) and bot (`xoxb-…`)
  tokens. Tokens never land in config files, git, or logs.
- **Log redaction is extended, not assumed**: the gateway redactor
  (`errorta_model_gateway/redaction.py`) does **not** currently match `xoxb`/`xapp` —
  the bridge adds those patterns (the `secret_scan.py` / `context/transforms`
  variants already cover Slack tokens and are the reference).
- **Channel linking is owner-confirmation** (`linking.py`): the F065 *state machine*
  (request → owner-approve → grant) is reused, but F065's TLS-cert-fingerprint +
  device-pubkey pinning does **not** apply to Slack. Trust instead rides **Slack's own
  request signing** (verify every payload's signature) **+ a Slack team-id/user-id
  allowlist**. Owner approval on desktop/CLI mints the capability grant; nothing acts
  before consent.
- **User allowlist + capabilities** (`auth.py`): only allowlisted Slack user ids (in
  the linked team) may drive the PM; **C**-class verbs can be gated to a narrower set.
  An unlisted user's message is ignored with an audit line — never a silent action.
- **C-class confirms require a verified Slack `block_actions` payload** (§4 ‡),
  resolved through the coding approval stores (`pm_changes` / governance / publish
  gate — not council F041). A confirm can never originate from concierge text.
- **Egress boundary preserved**: `errorta_slack` adds exactly one new outbound
  endpoint — the Slack Socket Mode WebSocket — and **no inbound port**. Any subprocess
  it needs (runtime launch) goes through the existing `errorta_tools` sandbox seam.
- **Instruction-source boundary**: Slack message *content* is data, not commands. The
  concierge maps text to its fixed tool surface only; an ungranted verb is rejected
  fail-closed and named back; and no text-derived call may resolve a decision or fire
  a C-class action (the injection fix, §4 ‡).

### 7.1 Slack token store & bridge-owned confirmation record

- **Token store** (`config.py` + a small `secrets.py`): a `0600` JSON file under
  `${ERRORTA_HOME}/slack/` (git-ignored), holding `app_token` / `bot_token`, modeled
  on `provider_keys.py`. Loaded only when the bridge is enabled; redacted everywhere.
- **Confirmation record** (`store.py`): because the coding path has no `run_id` to key
  the council F041 store, C-class staging uses a **bridge-owned durable pending record**
  (`{id, verb, args, thread_ts, created_at, state}`) that the verified button callback
  resolves. The *effect* of an approved confirm still executes against the real coding
  store (`pm_changes` / publish gate); this record is only the Slack-side staging key.

---

## 8. Error handling

- **Socket disconnect** → `connection.py` reconnects with capped backoff; inbound
  events Slack redelivers are deduped by event id (`store.py`); the outbound cursor
  guarantees at-least-once → exactly-once posting.
- **Concierge produces malformed JSON / an unknown verb** → rejected fail-closed,
  the allowed catalog is named back into a corrective follow-up turn (SPEC-17
  pattern); if it still fails, the PM posts a plain "I couldn't act on that —
  here's what I can do" with the verb list. Never a silent drop.
- **A tool call fails** (launch can't bind a port, queue write conflict) → the
  failure is surfaced verbatim-but-redacted in-thread ("couldn't start the preview:
  port in use — want me to retry?"), never swallowed.
- **`launch_runtime` with no `RuntimeProfile`** → empty-state reply, not a stale launch.
- **Model/budget unavailable for the concierge turn** → the bridge degrades to a
  deterministic status card for `project_status`/`recent_activity` (no model needed)
  and tells the user the conversational PM is offline.
- **Binding missing / project deleted** → the bot asks the user to (re)link rather
  than acting against a dangling project.

---

## 9. Testing

Everything is unit-testable without egress — the Slack client (Socket Mode), the
model caller, and the runtime launcher are all injected seams. No test uses a real
token or contacts Slack.

- **Concierge mapping tests** — table of (message, expected tool_calls) covering the
  three scenario messages + ambiguous/empty-state/hostile inputs. Model caller is a
  fake returning canned envelopes.
- **Injection tests** (review C5) — a concierge turn whose *text* says "approve the
  pending request" can **never** resolve a decision or fire a C-class action; only a
  verified `block_actions` payload can. Assert a forged/text-derived confirm id is
  rejected.
- **Trust-class tests** — `spend_cloud` / `publish_pr` never execute on first call;
  produce a bridge-owned confirmation record; execute only when re-invoked from a
  verified button callback; the effect lands in the coding store (`pm_changes` /
  publish gate), not council F041.
- **Outbound cursor tests** — a poll posts each new coding-state item exactly once; a
  crash between "posted" and "cursor advanced" does not double-post; a restart resumes
  at the cursor.
- **Auth/linking tests** — unlisted team/user ignored+audited; bad request signature
  rejected; link requires owner approval; grant minted only at approval; `xoxb`/`xapp`
  redaction holds across logs.
- **Connection tests** — Slack retry of the same `event_id` is deduped; ack is sent
  before processing; reconnect backoff is bounded.
- **Optionality tests** (§1) — with the Slack SDK absent / enable off, the sidecar
  boots, core routes work, and nothing imports `errorta_slack` in the boot path.
- **Anti-drift canary** — a test asserts the tool catalog the concierge is shown
  matches the verbs `tools.py` implements, so the prompt can't advertise a capability
  the code lacks.
- **`launch_runtime` tests** — no `RuntimeProfile` → empty-state, no process started;
  a configured profile → loopback URL returned, no public URL claimed.
- **Critical-block timeout tests** (§5.9) — a block with no reply nudges once, then on
  timeout auto-decides per the block's declared `on_timeout` tag; the two irreversible
  classes default to *don't spend* / *don't publish*; the decision writes through the
  coding approval store and is posted back; a human tap before timeout cancels it.
- **Concurrency tests** (§5.5) — per-`thread_ts` FIFO drain; a late "stop" cancels the
  in-flight action via the bounded look-ahead without reordering other messages.
- **Block scoping test** (§5.9) — a per-task critical block holds only that task and
  its dependents; independent tasks keep progressing; a run-wide decision halts all.
- **Ambiguity-signal test** (§5.4) — an under-specified message is acted on with the
  assumption stated first and the message tagged 🤔; a **C**-class ambiguity asks
  instead of guessing.
- **Thread-as-memory test** (§5.7) — a concierge turn reconstructs context from the
  Slack thread (its own prior post + the user reply), with no separate transcript;
  the durable store holds only bindings, outbound cursor, and explicit preferences.

---

## 10. Deferred to v2 (explicitly out of v1 scope)

Corrected after the architecture review surfaced hidden infrastructure. These are
**not built in v1**; v1 is honest about their absence.

- **Phone-openable public URL for `launch_runtime`** (review C1). Needs an
  authenticated public ingress (Tailscale Funnel / cloudflared) + a TTL auto-stop
  supervisor — real new infra. v1 returns the loopback/LAN URL and says so (§4.2).
- **Governed spec artifacts from `queue_bugs`** (review §5 hidden scope). v1 appends
  lightweight `todo` tasks via `ledger.add_task`. Turning a bug into a governed spec
  artifact (`source_spec_artifact_id`, the F100 governance store, a model call) is
  heavier and deferred.
- **A real push/subscribe event stream** for the coding team (review C2/C4). v1
  cursor-polls whole-log state; a proper tail/subscribe API would make outbound cheap.
- **Council-run bindings.** v1 binds a channel to a *coding* project. Extending the
  bridge to also govern *council deliberation* runs (which *do* use F041 + the event
  stream) is a separate scope.
- Multiple projects in one channel via threads (v1: strict one-channel-one-project).
- Slash-command fast-paths (`/errorta status`) as a cheap non-LLM shortcut.
