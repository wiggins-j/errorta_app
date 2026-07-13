# F158 — Talking to the PM from the CLI: interactive chat + ambient presence

**Target version:** v0.1 (CLI)
**Status:** proposed
**Owner:** wiggins-j

> Feature number is provisional — confirm against the F-registry before merge.

---

## Problem

In the desktop app, the PM feels like someone you can talk to: a "Contact the
PM" box where you type a question and its answer appears, a directive button
right next to it, a running chat transcript, and ambient surfacing (attention
cards, a PM-Changes badge) so the PM can reach *you* mid-run. From the CLI, all
the underlying capability exists — but only as **discrete one-shot commands**,
so the *experience* of a conversation is missing:

1. **No flowing conversation.** Every exchange is a separate shell command with
   its own quoting: `errorta pm ask "why did you drop caching?"`, read the
   reply, then `errorta pm ask "…"` again. There is no "sit down and talk to the
   PM" loop. `repl.py` has zero PM-conversation handling — `/pm …` falls through
   to generic `registry.dispatch` (`repl.py:67-68`), exactly like the argv form.
2. **The PM is invisible during a run.** The live view (`errorta run` /
   `log --watch`) polls a fixed source set (`poller.py` `DEFAULT_SOURCES`) with a
   fixed channel set (`verbosity.py` `CHANNEL_MIN_LEVEL`: `team-log, attention,
   prs, decisions, runtime, turns, tokens, tools, poll, http`). **There is no
   `pm` source or channel** — `GET /pm-chat` is read *only* by the explicit
   `pm chat` command. If the PM posts a message mid-run, you never see it while
   watching.
3. **`pm chat --watch` repaints** the whole transcript every tick (the snapshot
   watch path, `watch.py:69-91`) instead of tailing new turns the way
   `log --watch` does (the F151 stream path).

## What already works (do not rebuild)

The engine is **fully shared** with the app — **no engine/route change** is
needed. (One small **CLI-client** change *is* required — a longer per-call HTTP
timeout for `pm-ask`; see Item 1 / P0 below. So this is a CLI-only feature, but
not literally zero-code on the client.) The relevant routes and CLI commands
already exist:

| Capability | Route (`routes/coding.py`) | CLI today |
|---|---|---|
| Ask the PM, get a **synchronous** reply (≤120s), stored to a durable thread | `POST /projects/{id}/pm-ask` (1697) | `pm ask "…"` / bare `pm "…"` (`pm.py:90-107`) |
| Read the two-way transcript | `GET /projects/{id}/pm-chat` (1783) | `pm chat` (`pm.py:65-68`) |
| Authoritative directive (consumed next plan turn) | `POST /projects/{id}/interject` (1541) | `interject "…"` (`interject.py`) |
| Structured steering → reviewable PM Changes | `POST /projects/{id}/pm-control` (2046) | `pm control --actions '…'` |
| Review / revert PM Changes | `GET /pm-changes`, `.../accept`, `.../decline` | `pm changes` / `pm accept` / `pm decline` |
| PM → user "needs you" | `GET /projects/{id}/attention` (751) | `attention` (already a level-1 live channel) |

Note there is **no push and no streaming anywhere** — even the app polls
(project state 2.5s, PM Changes 4s, chat only on project switch). So "real-time"
is a polling-cadence concern, not a transport one.

## Goals

- A real **interactive PM conversation** from the CLI: enter a mode, see the
  transcript, type, get the PM's reply inline, keep talking — question *or*
  directive — the way the app's "Contact the PM" box feels.
- The PM becomes **present during a run**: new PM messages (and PM-initiated
  requests for input) surface in the live view instead of being invisible until
  you manually run `pm chat`.
- `pm chat --watch` tails new turns incrementally instead of repainting.

## Non-goals

- **No engine/route changes for v1.** Everything rides existing endpoints. The
  one client-side code change is a per-call HTTP timeout for `pm-ask` (P0); a
  dedicated "PM question to the user" signal kind (needed for the level-0 "needs
  you" headline) and any server push are follow-ups (see below).
- Not changing what `pm ask` / `interject` / `pm control` *do* — v1 is a
  presentation/UX layer over them. The one-shot commands stay for scripting.
- No multi-thread PM chat UI — v1 uses the default `thread_id="main"` (the route
  supports others; not surfaced yet).
- No LLM behavior changes (the PM's reply content, its agency to apply changes)
  — that is the shared engine.

---

## Item 1 — interactive PM chat mode

The headline. A back-and-forth conversation loop, reachable both ways:

- **argv:** `errorta pm chat -i` (or `errorta pm chat --interactive`).
- **REPL:** `/pm` with no args enters the same loop as a sub-mode; `/pm chat`
  (snapshot) is unchanged.

**Flow.**

1. On entry, fetch and render the existing transcript (`GET /pm-chat`, the same
   render as `pm chat`) so you pick up where you left off.
2. Drop into a readline loop with a distinct prompt, e.g. `pm ▸ `.
3. Each non-empty line is sent to the PM and its reply prints inline:
   - **plain line → a question** (`POST /pm-ask`): the conversational path. The
     reply text (`payload.reply.message`) prints under a `PM:` label; a spinner
     / "asking the PM…" shows while the bounded call is in flight.
   - **line prefixed `!` → a directive** (`POST /interject`): the authoritative
     path. Prints the delivered-directive confirmation, not a chat reply
     (mirrors the app's separate "Send directive" button). The `!` is stripped
     before sending.
   - If a reply (from either path) carried `applied` / `refusals` /
     `run_started`, print those lines (reuse `_applied_refusal_lines`, which
     lives in `commands/pm.py:185`) and, when changes were applied, a one-line
     hint: *"the PM changed config — review with `pm changes`."*
4. Meta-commands inside the loop: `/exit` (or Ctrl-D / Ctrl-C) leaves back to the
   shell / REPL; `/changes` prints pending PM Changes without leaving;
   `/help` prints the tiny in-loop cheat-sheet (question vs `!directive`, `/exit`).

**Design.** A new `_pm_interactive(client, ctx)` helper in `commands/pm.py`,
driven by the same readline primitive the REPL uses (factor the prompt-read out
of `repl.run_repl` so the loop is unit-testable with an injected `read_line`,
exactly as `wizard.run_wizard` already takes `read_line`/`write` —
`wizard.py:116`). Each turn calls the **existing** `_ask` / interject code paths
(same sole-owner guard, same routes, same renderers) — the loop is pure
orchestration, no new route logic. In the REPL, add `pm` (bare) to the builtins
dispatch in `repl.py:handle_line` so `/pm` enters the mode rather than being a
one-shot; `/pm chat`, `/pm ask …`, etc. keep dispatching as today.

**P0 — the HTTP timeout must cover a slow PM turn (a real client change).**
`SidecarClient` uses a single scalar read timeout of **30s**
(`client.py:40` `_DEFAULT_TIMEOUT = 30.0`), and `pm ask` posts with no override
(`pm.py:104-105`). But `pm_ask` server-side waits up to its configured bound —
**default 90s, capped at 120s** (`coding.py:1747-1750`). So any PM turn taking
>30s already raises an httpx read-timeout → `SidecarUnreachable`
(`client.py:100-103`) — i.e. the *existing* one-shot `pm ask` is silently broken
for slow turns, and the interactive loop inherits it. **Fix (v1, required):**
thread a per-call `timeout=` through `SidecarClient.request`/`post_json` and set
the `pm-ask` call to **≥130s** (the 120s server cap + margin). This is the one
non-presentation code change in F158; it also repairs one-shot `pm ask`. The
spec text says "≤120s" throughout; treat **90s default / 120s cap** as the
authoritative numbers.

**`--json` / non-interactive.** `-i` is a TTY-only affordance. Under `--json`,
a pipe, or `not is_interactive()`, `pm chat -i` refuses with a clean CliError
("interactive chat needs a terminal; use `pm ask \"…\"` for scripting") — never
hangs on a read. Mirrors the wizard's non-interactive guard
(`wizard.py:112-115`).

**Acceptance.** `errorta pm chat -i` on a bound project shows the transcript,
then a loop: a plain line returns the PM's answer inline; `!<text>` delivers a
directive; applied changes are surfaced with a `pm changes` hint; `/exit`
leaves. `--json`/piped refuses cleanly. The one-shot `pm ask` / `pm chat` are
byte-identical to today.

## Item 2 — ambient PM presence in the live run view

So the PM's messages show up while you watch a run, without a separate command.
This splits into a **v1 piece that needs no server change** and a **headline
piece that does** — the review found the original "needs you" headline had no
data source, so it's honestly separated here.

### Item 2a — the `pm` live channel (v1, no server change)

Add a **`pm-chat` poll source** to `poller.py` `DEFAULT_SOURCES` (append mode,
polling `GET /pm-chat`) mapped to a **new `pm` channel** in `verbosity.py`
`CHANNEL_MIN_LEVEL` at **level 1 (`default`)** — a PM message to you is at least
as important as a PR event. New PM turns (`role == "pm"`) since the last tick
print as `PM: …` lines in the live view; the user's own turns are not echoed
(they typed them). Reuse the content-LCP diff from the `log` tail
(`watch.py` `_entry_key` keys on `(at, role, member, kind, message)`, which
pm-chat turns satisfy) so nothing reprints. `/pm-chat` is an existing
poll-friendly read — this is pure poller + verbosity wiring.

**Acceptance (2a).** During `errorta run` (or `log --watch`) at default
verbosity, a PM message posted mid-run appears as a `PM:` line within one poll
interval; user turns are not echoed; no duplicate/reprinted PM lines; piped
output stays plain text. At `-V quiet` the `pm` channel is silent (level-gated,
like every other channel) — the loud "needs you" nudge is Item 2b.

### Item 2b — the "PM needs you" headline (OPTIONAL, requires an engine change)

**Why it can't be done in v1 as originally written.** The spec's first draft
promised a level-0 headline at `-V quiet` when the PM wants input. There is no
data or mechanism for that today:

- `pm_ask` only ever writes turns with `kind` `chat` / `error` / `unconfigured`
  (`coding.py:1721,1758,1777`) — never a "question/request-for-input" kind.
- Attention has exactly two kinds, `problem` / `alert`
  (`attention.py:30`), and a PM chat question **creates no attention signal**
  (`pm_ask` never touches attention) — so "fall back to a fresh attention
  signal" doesn't detect a PM-wants-input state.
- There is **no level-0 headline primitive in the channel stream** to piggyback
  on: `events_for_view` gates purely on `verbosity.should_emit(channel)`
  (`poller.py:252-256`), and existing blocker/terminal headlines come from the
  separate `GET /run` terminal-state path (`runstream.py`), not the channel
  feed. So a chat-driven quiet nudge is net-new runstream logic *with no
  trigger*.

**So Item 2b is deferred / optional and needs the engine.** To do it right, add
a first-class **`needs_input` (or `question`) signal** the PM raises when it
wants the user — most naturally a new `attention` kind (engine change in
`attention.py` + wherever the PM decides to ask), which then flows through the
already-polled `attention` channel and can drive a level-0 headline in
`runstream.py` naming `pm chat -i`. This is the clean backing and is scoped as a
follow-up (see Out of scope), not v1.

## Item 3 — `pm chat --watch` tails instead of repainting

`pm chat` is a growing transcript, so watching it should tail like `log`, not
repaint.

**Design.** Generalize the F151 stream machinery. Today `_run_stream` is
hard-wired to the team-log renderer (`from .render.log import filtered_entries,
render_entries` at `watch.py:123`, used at `:130` and `:143`). Factor the
entry-extraction and per-entry render behind the `Command` (e.g.
`stream_entries(payload) -> list` + `render_entries(entries) -> list[str]`,
defaulting to the log implementation) so a command can declare its own; feed the
pm-chat extractor `payload["thread"]` and reuse `render/pm.py:_render_chat`'s
per-entry formatting. The LCP diff (`watch.py:_entry_key`) handles the
transcript's append-mostly shape; a turn that mutates in place degrades to a
bounded reprint, never a dropped turn.

**Sub-verb hazard (the crux).** `watch_mode` is a field on the whole `Command`
and `run_watch` reads `command.watch_mode` (`watch.py:66`) — it never inspects
the sub-verb. But `pm` is **one** registry `Command` (`pm.py:199`) with both a
tail-able `chat` sub and snapshot subs (`changes`). Setting `watch_mode="stream"`
on the `pm` command would wrongly route `pm changes --watch` through the tail.
So the fix is **not** a field on `pm.py`; it's making the shared watch layer
sub-aware — e.g. give `Command` an optional `watch_mode_for(args) -> str` hook
(default returns the static `watch_mode`) and have `run_watch` call it with the
resolved `args`, so `pm` returns `"stream"` only when the sub is `chat`. The
steering subs stay un-watchable via the existing `_reject_watched_mutation`
(`pm.py:45-51`).

**Acceptance.** `errorta pm chat --watch` appends new turns as they arrive, never
reprints the whole transcript, exits cleanly on Ctrl-C, stays plain text when
piped. `log --watch` is unaffected (same shared path).

---

## Implementation notes

- **P0 (client timeout)** — `client.py`: add an optional per-call `timeout=` to
  `request`/`post_json` (thread into the httpx call); `commands/pm.py` passes
  `timeout≈130` on the `pm-ask` POST. Repairs one-shot `pm ask` too. Small, but
  it's the one non-presentation change.
- **Item 1** — `commands/pm.py`: `_pm_interactive` loop + a `-i`/`--interactive`
  `Param`; factor `read_line`/`write` out of `repl.run_repl` (reuse the wizard's
  `read_line`/`write` pattern, `wizard.py:116`) for testability; `repl.py`
  `handle_line` routes bare `/pm` into the loop. No route changes. Sole-owner
  guard + confirm already live on the underlying `_ask`/interject paths.
- **Item 2a** — `poller.py`: add an append-mode `pm-chat` source; `verbosity.py`:
  add the `pm` channel at level 1; `runstream.py`: render new PM turns via the
  LCP-diff helper. No new headline logic (that's 2b).
- **Item 2b (optional, engine)** — `attention.py`: a new `needs_input` kind + the
  PM raising it; `runstream.py`: a level-0 headline off that signal. Deferred; do
  not fold into the v1 PR.
- **Item 3** — generalize `_run_stream` (entry extractor + per-entry renderer
  behind the `Command`, hard-wire at `watch.py:123/130/143`); add a
  `watch_mode_for(args)` hook on `Command` and have `run_watch` consult it
  (`watch.py:66`) so `pm` streams **only** when the sub is `chat`; `commands/log.py`
  unchanged. Steering subs stay un-watchable (`_reject_watched_mutation`,
  `pm.py:45-51`).
- **cli.spec** — no new bundled modules; all changes are in already-packaged
  files.

## Edge cases

- **PM unreachable / mid-turn — branch on `answered is False`, not on `error`.**
  `pm-ask` has **two** non-answer branches: `answered:false,
  error:"pm_unreachable"` when the PM is busy/unreachable (`coding.py:1763`), and
  `answered:false, kind:"unconfigured"` with **no `error` key** when no team is
  set up (`coding.py:1719-1728`). A loop that keys only on
  `error == "pm_unreachable"` would misclassify the second (or KeyError on it).
  So: treat any `answered is False` as "not a real reply", then message off
  `reply.kind` / `error` (busy → "try again in a moment"; unconfigured → "assemble
  a team first: `errorta team apply --yes`"). Stay in the loop either way; the
  user turn is persisted before the model call (`coding.py:1732`), so nothing is
  lost.
- **Long PM calls.** The route waits up to its bound (default 90s, cap 120s);
  the client timeout must exceed that (P0, ≈130s) or the ask dies early. Show
  elapsed / a cancel-with-Ctrl-C affordance so the loop never looks hung. Ctrl-C
  during an in-flight ask aborts that turn and returns to the prompt, not out of
  the mode — but note the server already recorded the user turn and may still be
  running the model, so the reply is simply lost to the client (it will appear in
  the transcript on the next `pm chat`).
- **No run active.** Chat still works (the PM answers about state); a directive
  that implies work is delivered and picked up when a run next starts (unchanged
  interject semantics).
- **Three input grammars on one line-reader.** The loop reads bare = question,
  `!…` = directive, `/…` = meta (`/exit`, `/changes`, `/help`). Two collisions to
  pin down and document: (a) a question that legitimately *starts* with `!` — use
  a leading `! ` (bang-**space**) as the directive sigil so `!important, why…?`
  is still a question, or accept `\!` to escape; (b) a question that starts with
  `/` (e.g. "what's in `/etc/hosts`?") — only treat a leading token that matches a
  known meta-verb (`/exit|/changes|/help`) as meta; anything else starting with
  `/` is sent as a question. Pick and document one rule for each.
- **Interleaving with the run live view.** If a run is streaming in the same
  terminal, entering `pm chat -i` is a foreground takeover (like any REPL
  command); Item 2 is what keeps the PM visible *without* leaving the run view.
- **Item 2 duplicate suppression** across a reconnect / empty tick must not
  reprint the tail or lose the cursor (same guarantees as the F151 log tail).

## Testing

- **P0**: a mocked slow `pm-ask` (server responds after >30s, simulated) does
  NOT raise `SidecarUnreachable` once the per-call timeout is set; a one-shot
  `pm ask` gets the same longer timeout.
- **Item 1** (deterministic, injected `read_line`/`write`, mocked client): a
  scripted session — a question line hits `POST /pm-ask` and prints the reply; a
  `!directive` line hits `POST /interject` and prints the delivered
  confirmation; an applied-change reply prints the `pm changes` hint; both
  `answered:false` branches (`pm_unreachable` and `unconfigured`, the latter with
  no `error` key) are handled without KeyError and keep the loop alive; `/exit`
  ends the loop; a question starting with `/` or `!` (non-meta) is sent, not
  swallowed; `--json`/non-interactive refuses without reading. Assert the
  underlying routes/guards are the same objects as the one-shot path (parity).
- **Item 2a**: growing `/pm-chat` payloads across ticks → new `PM:` lines print
  once, in order, no reprints; user turns are not echoed; the `pm` channel is
  silent at `-V quiet` and shown at `default`; piped output has no ANSI.
- **Item 3**: reuse the F151 log-tail test shape for `pm chat --watch`
  (append-once, quiet tick prints nothing, reset reprints, piped plain text);
  crucially assert `pm changes --watch` still uses the **snapshot** path (the
  sub-aware `watch_mode_for`), and `pm` steering subs remain un-watchable.
- Full CLI suite + `ruff`; frozen-binary smoke test of `pm chat -i`.

## Documentation

- `docs/CLI.md` — Mid-run steering guide: a "Talking to the PM" subsection
  (question vs directive, `pm chat -i`, the in-loop meta-commands); note the new
  `pm` live channel in the verbosity table (level 1); note `pm chat --watch`
  tails.
- `README.md` — the steering snippet can show `errorta pm chat -i` as the way to
  "talk to your PM."
- Keep `--help` usage strings in sync.

## Out of scope / follow-ups

- **Item 2b — the "PM needs you" headline + its `needs_input` signal kind**
  (engine change to `attention.py`, whose kinds are today only `problem`/`alert`,
  plus the PM raising it and a level-0 headline in `runstream.py`). Pulled out of
  v1 because there is no existing trigger for it (see Item 2b); it's the clean
  backing for the quiet-verbosity nudge and can land as its own follow-up PR.
- **Server push / SSE** for PM turns (removes the poll cadence entirely) — a
  cross-cutting transport change well beyond this feature.
- **`?since=<cursor>` on `/pm-chat`** (bandwidth) — same follow-up shape as the
  team-log tail cursor noted in F151.
- **Multi-thread PM chat** (the route already takes `thread_id`).
- **Multi-line composer** in the interactive loop (paste a paragraph) — v1 is
  line-per-turn.
