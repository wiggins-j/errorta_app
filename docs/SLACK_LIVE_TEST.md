# Slack PM Bridge — live test runbook

A step-by-step to take the **optional** Slack bridge from nothing to a real
message answered by your Errorta coding-team PM. Everything the bridge needs is
listed; nothing here is on by default.

> **Before you rely on it, run the preflight (Step 6).** It validates the whole
> setup and, with `--connect`, proves your tokens + Socket Mode connectivity
> *before* you send a real message.

---

## 0. Prerequisites
- A Slack workspace where you can create + install an app.
- Errorta built/installed with the Slack extra: `pip install '.[slack]'` (or
  `pip install 'errorta-app[slack]'`). This pulls `slack-sdk`.
- At least one **coding project** already created in Errorta (the bridge binds a
  channel to a project — it doesn't create one).
- A model the coding team can use (the concierge answers via the local gateway).

## 1. Create the Slack app from the manifest
1. Go to <https://api.slack.com/apps> → **Create New App** → **From an app manifest**.
2. Pick your workspace, paste [`docs/slack-app-manifest.yaml`](slack-app-manifest.yaml), **Create**.

## 2. Install + get the two tokens (you do this — they are secrets)
1. **Bot token** (`xoxb-…`): *OAuth & Permissions* → **Install to Workspace** →
   Allow → copy **Bot User OAuth Token**.
2. **App-level token** (`xapp-…`): *Basic Information* → **App-Level Tokens** →
   **Generate**, add the **`connections:write`** scope, copy it.

## 3. Store the tokens (0600 file — never in shell history)
The bridge reads tokens from a `0600` file via `errorta_slack.secrets`. Store them
by **pasting when prompted** (do not put tokens on the command line):

```bash
cd python && .venv/bin/python - <<'PY'
import getpass
from errorta_slack import secrets
app = getpass.getpass('xapp- app-level token: ').strip()
bot = getpass.getpass('xoxb- bot token: ').strip()
secrets.save_tokens(app, bot)
print('saved 0600 to', secrets.path())
print('masked:', secrets.mask())
PY
```

## 4. Allowlist yourself + enable the bridge
Only allowlisted Slack users can drive the PM (empty allowlist = deny-all). Add
your Slack **team id** and **user id** to the config, then enable:

```bash
cd python && .venv/bin/python - <<'PY'
from errorta_slack import config
cfg = config.load()
cfg['allowed_team_ids'] = ['T_YOUR_TEAM_ID']
cfg['allowed_user_ids'] = ['U_YOUR_USER_ID']
cfg['enabled'] = True
config.save(cfg)
print('enabled; allowlist set')
PY
```
(Find your ids in Slack: profile → *Copy member ID* for the user; the team id is
in the app's *Basic Information* or any workspace URL.)

## 5. Link a channel to a project
1. Create/choose a channel and **invite the bot**: `/invite @errorta-pm`.
2. Bind that channel to a coding project (owner-confirmation model):

```bash
cd python && .venv/bin/python - <<'PY'
from errorta_slack import linking
link_id = linking.request_link('C_YOUR_CHANNEL_ID', 'YOUR_PROJECT_ID', 'U_YOUR_USER_ID')
linking.approve_link(link_id)   # you are the owner approving
print('linked channel -> project')
PY
```

## 6. Preflight — verify BEFORE messaging
```bash
cd python && .venv/bin/python -m errorta_slack.preflight            # local checks only
cd python && .venv/bin/python -m errorta_slack.preflight --connect  # + real auth.test + Socket Mode probe
```
Every line should be ✅. `--connect` confirms the tokens are valid and the
sidecar can actually reach Slack over Socket Mode (it connects for a few seconds
and disconnects; it posts nothing).

## 7. Start the sidecar and send a message
Start the sidecar (the bridge auto-starts because it's enabled with tokens
present). Then, in the linked channel:

> **you:** how's the project going?

You should get a status reply. Try: *"spin up the code, send me the link"* and
*"add a bug: the header overlaps on mobile"*. Decisions arrive as messages with
**Approve/Decline** buttons.

---

## Known v1 limits (so nothing surprises you)
- **Launch links are loopback** (`http://127.0.0.1:<port>`) — open them on the
  same machine, or reach them via your own network/tunnel. No public URL (v2).
- **Thread memory is in-process** and **resets when the sidecar restarts** — the
  PM won't remember earlier messages across a restart yet (durable memory is a
  tracked follow-up).
- Bugs become lightweight `todo` tasks, not governed spec artifacts (v2).
- Spending on cloud models and opening a public PR always require a button tap;
  other timed-out decisions are auto-decided conservatively.

## Turning it off
Set `enabled=False` in config (or delete the token file). The bridge stops on the
next sidecar boot; the app and CLI are unaffected either way.
