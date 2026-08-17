# Slack studio manager (`errorta_slack`)

An extension of the [Slack PM bridge](SLACK_PM_BRIDGE.md): a single
**app-level** PM that lives in one dedicated "studio" channel, separate from
any per-project channel. Chat it a north star for something new — "let's
build a habit tracker for climbers" — and it gathers the rest of the charter
conversationally, then, once you approve, creates the coding project *and*
its own dedicated Slack channel for you to move into.

> **Strictly optional, same as the rest of the bridge.** With no studio
> channel bound, the bridge behaves exactly as it does today — the studio
> manager only activates in the one channel you explicitly designate.

---

## What it is

- The studio manager is the **same live bridge connection** as the
  per-project PM, just routed differently: any message posted in the
  designated studio channel is handled by a separate "studio concierge" turn
  instead of a project's PM turn.
- Its one bounded tool surface (`python/errorta_slack/studio_tools.py`):
  `list_projects` (see every project this studio has created), `create_project`
  (gather a charter, then create the project and its channel), and
  `answer_question`.
- `create_project` is the studio's only **C**-class (irreversible) verb — it
  creates a project *and* a public Slack channel. Like the per-project
  bridge's spend/publish actions, it always stages a confirmation first;
  it only ever executes after a verified **Approve** button tap, never from
  parsed chat text.

## Designate the studio channel

Pick (or create) a Slack channel you want to use as your studio home, invite
the bot to it, then bind it against the sidecar's local API:

```
curl -X POST http://<sidecar-host>:<port>/slack/studio/bind \
  -H 'Content-Type: application/json' \
  -d '{"channel_id": "<CHANNEL_ID>"}'
```

`<CHANNEL_ID>` is the Slack channel id (starts with `C…`), not its display
name — find it via *"Copy link"* on the channel in the Slack client, or
`GET /slack/status`/your own workspace tooling. Binding again overwrites the
previous studio channel (it's a singleton, one studio channel at a time).

Check the current binding any time:

```
curl http://<sidecar-host>:<port>/slack/studio
```

which returns `{"studio_channel": "<CHANNEL_ID>"}` (or `null` if never set).

This only designates *which* channel the studio manager listens on — it does
not itself enable the bridge; you still need `POST /slack/enable` with tokens
on disk, per the base [PM bridge doc](SLACK_PM_BRIDGE.md#enable-the-bridge).

## New scope: `channels:manage`

Creating a project's Slack channel goes through Slack's
`conversations.create` / `conversations.invite` / `conversations.setTopic`
calls, which require the **`channels:manage`** bot token scope in addition
to the scopes the base PM bridge already needs. It's included in
[`docs/slack-app-manifest.yaml`](slack-app-manifest.yaml).

If your Slack app already exists from setting up the base PM bridge, add the
scope under *Features → OAuth & Permissions → Scopes → Bot Token Scopes*,
then **reinstall the app to your workspace** — Slack requires a reinstall
any time a bot's scope set changes, even though the bot token string itself
doesn't change.

## The create flow

1. In the studio channel, describe what you want to build in plain
   language — a north star is enough to start ("a Slack bot that reminds my
   team to water the office plants").
2. The studio manager asks whatever follow-up questions it needs (audience,
   what "done" looks like, where it runs, etc.) across the thread, the same
   conversational way the per-project PM works.
3. Once it has enough to propose a charter, it posts a decision message with
   an **Approve** / **Decline** button pair — nothing is created yet.
4. Tapping **Approve** is the one verified action that actually executes
   `create_project`: it creates the coding project first, then its Slack
   channel, invites you, and sets the topic from your stated north star.
   The new project channel then behaves exactly like any other bound
   project channel — same PM, same tool surface, same trust model.
5. Tapping **Decline** (or letting the confirmation time out) creates
   nothing; you can just keep chatting to revise the charter.

## Spin a project down

Tell the studio manager to spin a project down (e.g. *"shut down the homeschool
game project"*) → `archive_project`. This is a **C-class** action — it changes
real state, so the manager posts an **Approve** button and only your tap runs it.
It is a **reversible soft spin-down**:

- If a run is live, it requests a graceful **cancel**.
- It sets the project to **paused** (the project and its history are kept — this
  is not a delete).
- It **archives the project's Slack channel** and drops the channel↔project
  binding.

Hard delete (destroying the project's workspace and ledger) is **not** in this
slice — spin-down is the reversible option.

## Reconfigure the team

That's done in the **project's own channel**, not here — see *Reconfigure the
team* in `SLACK_PM_BRIDGE.md` ("switch the reviewer to opus").

## v1 scope

- **Project created IDLE, then you start it.** `create_project` creates the
  project + channel but doesn't start a run. In the new project channel, tell
  the PM to **start building** (see *Run control* in `SLACK_PM_BRIDGE.md`) — an
  Approve button kicks off the coding team.
- **Public channels only.** The channel the studio manager creates is a
  public channel; private-channel provisioning is not in v1.
- **One studio channel.** Like project bindings, the studio channel is a
  singleton — binding a new one replaces the old one, it doesn't add a
  second studio surface.
- **Public channels only.** The channel the studio manager creates is a
  public channel; private-channel provisioning is not in v1.
- **One studio channel.** Like project bindings, the studio channel is a
  singleton — binding a new one replaces the old one, it doesn't add a
  second studio surface.

## Public-repo hygiene

Same discipline as the rest of this package: no real Slack token, channel
id, hostname, or workspace name appears in this doc or its tests — every
example above uses a placeholder (`<CHANNEL_ID>`, `<sidecar-host>:<port>`).
