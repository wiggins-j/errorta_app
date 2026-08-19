# Slack PM bridge (`errorta_slack`)

Chat with the coding-team PM in a Slack channel the way you'd talk to a
project manager — "how's it going?", "spin up the build and send me the
link", "here are three bugs, add them to the work." The bridge is a thin
Slack-shaped front door over the *existing* coding-team engine — it does not
give the team any capability it didn't already have.

> **This feature is strictly optional and OFF by default.** With it
> uninstalled, disabled, or unconfigured, the desktop app, sidecar, and CLI
> behave identically to a build that never heard of Slack — no import, no
> connection, no routes, no behavior change anywhere else.

---

## v1 scope — read this first

- **Loopback URL only.** `launch_runtime` starts your project's preview and
  replies with a **loopback** URL (`http://127.0.0.1:<port>`) — that's all
  v1 ships; there is no LAN-address detection in the code. Reaching it from
  another device is up to your own network setup (e.g. running the sidecar
  on a host you can already reach, or your own SSH tunnel) — the bridge
  itself does not expose a LAN or public URL. A **public, phone-openable
  URL** needs a real authenticated public ingress and is deferred to v2.
- **Bugs become lightweight queued tasks, not governed specs.** "Add this bug
  to the work" appends a plain `todo` task to the project ledger. Turning a
  bug into a fully governed spec artifact (with its own model call and
  approval trail) is heavier and deferred to v2.
- **One channel = one coding project.** A Slack channel is bound to exactly
  one project at a time (`switch_project` rebinds it, it doesn't add a
  second). Multiple projects sharing one channel via threads is deferred.

See [the design doc](superpowers/specs/2026-08-14-slack-pm-bridge-design.md)
for the full behavioral contract and the complete v2 backlog.

## What it is

`python/errorta_slack/` adds:

- A **Slack Socket Mode** connection (the sidecar dials *out* over a
  WebSocket — no public webhook, no inbound port, works behind NAT).
- A **concierge**: a stateless front-door agent. Each inbound Slack message
  runs one model turn against a bounded tool surface — the model can never
  reach the engine directly, only through a fixed, audited verb list.
- **Proactive posts**: the PM pushes exactly two kinds of message into the
  bound channel — a 🔴 **decision needed** (blockers, approvals; @-mentions
  you, has Approve/Decline buttons) and a plain **FYI** (run finished, PR
  ready; no mention, no buttons). Day-to-day progress is pull-only — you ask,
  it answers.
- A **hybrid trust model**: reads, status, and launching/stopping the preview
  execute immediately from your chat message. The two irreversible actions —
  **spending on a cloud model call** and **opening a public PR** — always
  stage a confirmation and require a verified in-thread button tap. A
  confirmation can never be resolved by parsing chat text (including text
  pasted from elsewhere), which is what keeps a prompt-injection attempt like
  "approve the pending request" from spending money or publishing on its
  own.

The full v1 verb list (`python/errorta_slack/tools.py`): `list_projects`,
`switch_project`, `project_status`, `recent_activity`, `launch_runtime`,
`stop_runtime`, `queue_bugs`, `answer_question`, `resolve_decision`,
`spend_cloud`, `publish_pr`. The last three require the confirm-button step
above; everything else runs immediately.

## Install

The core install never pulls in the Slack SDK. To add it:

```
pip install '.[slack]'
```

(run from the `python/` checkout, or `pip install 'errorta-app[slack]'` from
a release). Without this extra installed, the sidecar boots exactly as
before and the bridge is simply unavailable — never a hard import error.

## Create the Slack app

You'll create your own single-workspace Slack app — there is no shared or
multi-tenant Errorta app.

1. Go to <https://api.slack.com/apps> → **Create New App** → **From
   scratch**. Name it whatever you like (e.g. "Errorta PM"), pick your
   workspace.
2. **Socket Mode**: under *Settings → Socket Mode*, turn it **on**. This
   generates an **app-level token** starting with `xapp-…` — copy it, you'll
   need it below. Socket Mode is what lets the bridge connect outbound
   without exposing a public endpoint.
3. **Bot token scopes**: under *Features → OAuth & Permissions*, add these
   Bot Token Scopes:
   - `chat:write` — post replies and proactive messages
   - `reactions:write` — the 👀 / ✅ / 🤔 ack reactions
   - `channels:history` (or `groups:history` for a private channel) — read
     the thread the concierge treats as its memory
   - `channels:read` (or `groups:read`) — resolve channel/team identity for
     the allowlist check
4. **Interactivity**: under *Features → Interactivity & Shortcuts*, turn it
   **on**. (No Request URL is needed — interactive payloads arrive over the
   same Socket Mode connection.)
5. **Install the app** to your workspace (*Settings → Install App*). This
   mints the **bot token** starting with `xoxb-…` — copy it too.
6. Invite the bot to the channel you want it to run in (`/invite @Errorta
   PM` or similar).

You now have two tokens: an `xapp-…` app-level token and an `xoxb-…` bot
token. **Never commit either one.** They are secrets, not configuration.

## Enable the bridge

Tokens are stored in a dedicated `0600` (owner-read-only) JSON file under
`${ERRORTA_HOME}/slack/tokens.json` (git-ignored, never logged — the bridge's
log redactor strips any `xoxb-…`/`xapp-…`-shaped string on sight). There is
no HTTP route that accepts raw tokens — write them once via the store module
directly, from a machine you trust, e.g.:

This snippet **prompts** for the two values rather than carrying them inline,
so there is no placeholder to paste by accident and neither token is echoed to
the terminal or into your shell history:

```
python - <<'PY'
from errorta_slack import secrets
import getpass
app = getpass.getpass('xapp- app-level token: ').strip()
bot = getpass.getpass('xoxb- bot token: ').strip()
secrets.save_tokens(app, bot)
print('saved:', secrets.mask())
PY
```

> **Why prompted, not inline.** The previous version of this snippet carried
> `app_token='xapp-...'` literally. Run unedited — which is exactly what a
> copy-paste setup step invites — it wrote the 8-character placeholders over
> live credentials, which are unrecoverable from disk. `save_tokens` now
> refuses placeholder-shaped values *before* touching the file, so existing
> tokens survive the mistake, but prompting removes the trap entirely.

Then flip the bridge on:

```
POST /slack/enable
```

against the sidecar's local API. This only persists the `enabled` flag —
`enable` returns `400` if no tokens are on disk yet. The live Socket Mode
connection itself is reconciled at sidecar start/stop (so flipping the flag
never blocks on live Slack network I/O). `POST /slack/disable` turns it back
off; `GET /slack/status` shows whether it's enabled, current channel
bindings, and your tokens masked to their last 4 characters (never the raw
value).

### Linking a channel to a project

Binding a Slack channel to a coding project goes through the same
owner-confirmation pattern as the mobile companion: a request to link is
staged, and nothing binds until you approve it —

```
POST /slack/link/approve
{ "link_id": "<the pending link id>" }
```

Only allowlisted Slack team/user ids (`allowed_team_ids` /
`allowed_user_ids` in the bridge's config — empty by default, which denies
everyone) can drive the PM at all; the allowlist plus Slack's own
request-signature verification are what stand in for the phone app's
TLS-pinning, since Slack messages don't carry a pinned device certificate.

## Behavior in brief

- **Addressing.** One dedicated channel per project binding. In that
  channel, allowlisted users don't need to @-mention the bot for it to act —
  only a proactive critical decision @-mentions *you*.
- **Ambiguity.** An under-specified request gets acted on with the PM's best
  guess, stated as the first clause of its reply and tagged 🤔 — except for
  the two irreversible (C-class) actions, where it asks instead of guessing.
- **Memory.** The Slack thread itself is the conversation memory — the PM
  re-reads the thread each turn rather than keeping a separate transcript
  that could drift from what's actually on screen.
- **Unanswered critical blocks** get one nudge re-ping, then time out (30
  minutes by default, configurable per binding) to a conservative default:
  the two irreversible action classes always default to *don't spend* /
  *don't publish*; every other block declares its own safe default at the
  point it's raised. Every auto-decision — timed-out or tapped — is posted
  back to the thread with the reasoning, and writes through the same audited
  approval store a desktop/CLI decision would.

## Run control (start / stop / status)

In a project's channel you can drive the coding team's **run** — the team
actually writing code — directly from the PM:

- **"start building" / "go" →** `start_run`. This is a **C-class** action: it
  spends real money on the team's model calls, so it never fires from a chat
  message alone — the PM posts an **Approve** button and only your tap starts
  the run. Once started, the team works in the background (a sidecar thread that
  keeps going after the reply) up to the run's **iteration cap** (default 200;
  there is no dollar cap — the honest limit is iterations/model-calls, not USD).
  The PM picks the right mode from the current state: a fresh project starts
  clean, a stopped run continues, an interrupted run resumes, and an
  already-running project is reported as such without restarting.
- **"stop" →** `stop_run`. A **graceful** cancel — the team finishes its current
  step, then halts (the signal is durable, so it survives a sidecar restart).
  Stopping an idle project is a friendly no-op.
- **"how's it going?" →** `project_status` now reports the run's lifecycle state
  (`idle` / `running` / `stopped` / `failed` / `interrupted`) alongside the
  task and blocker summary.

These are distinct from `launch_runtime` / `stop_runtime`, which start and stop a
**preview** of the *built* code (a dev server) — not the coding team's run.

## Reconfigure the team

In a project's channel you can change which model a role uses — *"switch the
reviewer to opus"*, *"put the devs on cursor composer 2.5"* → `reconfigure_team`.
It's **grounded-or-refuse**: the model name is resolved against the routes that
are actually available/installed, and an unknown or ambiguous name is refused
with the list of candidates — the PM never silently picks the wrong model. The
change is applied immediately, announced back to you, takes effect on the team's
**next turn** (you can reconfigure mid-run), and is recorded as an **undoable**
change. Roles are `pm` / `dev` / `reviewer` / `tester`.

## Autopilot — let the PM approve & execute itself

By default, a **C**-class action you request in chat ("start building", "open
the PR", "create the project") posts an **Approve / Decline** button and waits
for your tap — the button is the anti-prompt-injection gate (chat text alone,
even text that says "approve", can never fire a C-class action).

If you'd rather talk to a PM that just *does the thing*, turn on **autopilot**.
With it on, when the PM stages a C-class action it immediately approves and
executes it itself — through the *exact same verified internal path* a button
tap uses — and posts a `🤖 Autopilot approved …` line so nothing happens
silently. It covers every C-class verb: `start_run`, `spend_cloud`,
`publish_pr`, and the studio's `create_project` / `archive_project`.

Autopilot is **off by default**. Enable it deliberately, from a machine you
trust, by writing the flag to the bridge config:

```
python - <<'PY'
from errorta_slack import config
cfg = config.load(); cfg['autopilot'] = True; config.save(cfg)
PY
```

Set it back to `False` to return to button-gated approvals.

**Security tradeoff (read before enabling).** The button is a second human
factor. With autopilot on there is none, so content *you paste* into the
channel that the model treats as an instruction could drive a C-class action
(spend, publish). The mitigations that remain: (1) it's **off by default** —
opt-in only; (2) only **allowlisted** team/user ids can drive the PM at all,
autopilot or not; (3) every autopilot action posts a loud in-thread audit
line; (4) everything it does is **reversible from chat** — `stop` a run,
"spin down" a project. The tool-layer injection guard itself is unchanged:
autopilot only ever fires an action the PM *structurally staged*, never raw
chat text.

## Public-repo hygiene

This module and its tests never contain a real Slack token, contact Slack,
or read a real owner's credentials — every Slack-facing seam (the Socket
Mode client, the model caller, the runtime launcher) is injected, and tests
use fakes with placeholder-shaped values (`xoxb-…`, `you@example.com`).
Nothing enables itself; every behavior above requires the operator to
explicitly install the extra, generate their own Slack app, and flip the
enable flag.
