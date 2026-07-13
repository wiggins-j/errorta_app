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
   PM" loop. `repl.py` has zero PM-conversation handling — `/pm …` is dispatched
   exactly like the argv form (`repl.py:53-59`).
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

The engine is **fully shared** with the app — no server change is needed for
this feature. The relevant routes and CLI commands already exist:

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

- **No engine/route changes for v1.** Everything rides existing endpoints. A
  dedicated "PM question to the user" signal kind and any server push are
  follow-ups (see below).
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
     `run_started`, print those lines (reuse `render/pm.py:_applied_refusal_lines`)
     and, when changes were applied, a one-line hint: *"the PM changed config —
     review with `pm changes`."*
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

So the PM can reach you while you watch a run, without a separate command.

**Design.**

- Add a **`pm-chat` poll source** to `poller.py` `DEFAULT_SOURCES` (polls
  `GET /pm-chat`) mapped to a **new `pm` channel** in `verbosity.py`
  `CHANNEL_MIN_LEVEL`. Proposed level: **1 (`default`)** — a PM message to you is
  at least as important as a PR event. New PM turns (`role == "pm"`) that appear
  since the last tick print as `PM: …` lines in the live view; the user's own
  turns are not echoed (they typed them). Use the same content-LCP diff as the
  `log` tail (F151 `watch.py:_run_stream`) so nothing reprints.
- **"PM needs you" headline.** A PM turn whose `kind` marks it as a
  question/request-for-input (or, until such a `kind` exists, a fresh unresolved
  `attention` signal — attention is already polled at level 1) prints a
  **level-0 headline** (like blockers / run start), so even at `quiet` you're
  told the PM is waiting on you, with the exact command to answer
  (`errorta pm chat -i`).

**No new route.** Both `/pm-chat` and `/attention` are existing polled-friendly
reads; this is wiring them into the poller + verbosity model.

**Acceptance.** During `errorta run` (or `errorta log --watch`) at default
verbosity, a PM message posted mid-run appears as a `PM:` line within one poll
interval; at `-V quiet` a PM request-for-input still surfaces as a one-line
headline naming `pm chat -i`. No duplicate/reprinted PM lines. Piped output
stays plain text.

## Item 3 — `pm chat --watch` tails instead of repainting

`pm chat` is a growing transcript, so watching it should tail like `log`, not
repaint.

**Design.** Generalize the F151 stream machinery. Today `watch.py:_run_stream`
is hard-wired to the team-log renderer (`from .render.log import
filtered_entries, render_entries`, `watch.py:117`). Factor the entry-extraction
and per-entry render behind the `Command` (e.g. `stream_entries(payload) ->
list` + `render_entries(entries) -> list[str]`, defaulting to the log
implementation) so a command can declare its own. Give `pm chat`
`watch_mode="stream"` with a pm-chat entry extractor (`payload["thread"]`) and a
per-turn renderer (reuse `render/pm.py:_render_chat`'s per-entry formatting).
The LCP diff handles the transcript's append-mostly shape; a PM turn that
mutates in place degrades to a bounded reprint, never a dropped turn.

**Acceptance.** `errorta pm chat --watch` appends new turns as they arrive, never
reprints the whole transcript, exits cleanly on Ctrl-C, stays plain text when
piped. `log --watch` is unaffected (same shared path).

---

## Implementation notes

- **Item 1** — `commands/pm.py`: `_pm_interactive` loop + a `-i`/`--interactive`
  `Param`; factor `read_line`/`write` out of `repl.run_repl` (reuse the wizard's
  pattern) for testability; `repl.py:handle_line` routes bare `/pm` into the
  loop. No route changes. Sole-owner guard + confirm already live on the
  underlying `_ask`/interject paths.
- **Item 2** — `poller.py`: add the `pm-chat` source; `verbosity.py`: add the
  `pm` channel at level 1 and the level-0 "needs you" headline rule;
  `runstream.py`: render new PM turns via the LCP-diff helper. Attention is
  already surfaced at level 1 — the only new headline is the low-verbosity nudge.
- **Item 3** — generalize `watch.py:_run_stream` (entry extractor + per-entry
  renderer behind the `Command`); `commands/log.py` keeps its current behavior;
  `commands/pm.py` sets `watch_mode="stream"` for the `chat` sub-view only.
  Careful: `pm` is one registry command with multiple sub-verbs, so the stream
  mode must key on the `chat` sub, not the whole command (the steering subs stay
  un-watchable — `_reject_watched_mutation`, `pm.py:45-51`).
- **cli.spec** — no new bundled modules; all changes are in already-packaged
  files.

## Edge cases

- **PM unreachable / mid-turn.** `pm-ask` returns `answered:false,
  error:"pm_unreachable"` (coding.py:1763), not a 500. The loop prints a friendly
  "the PM is busy on its turn — try again in a moment" and stays in the loop; the
  user turn is already persisted server-side (append-before-call), so nothing is
  lost.
- **Long PM calls.** The route bounds the wait to ≤120s; show elapsed / a
  cancel-with-Ctrl-C affordance so the loop never looks hung. Ctrl-C during an
  in-flight ask aborts that turn and returns to the prompt, not out of the mode.
- **No run active.** Chat still works (the PM answers about state); a directive
  that implies work is delivered and picked up when a run next starts (unchanged
  interject semantics).
- **`!` as literal text.** A user who wants to *ask* a question starting with `!`
  can escape it (`\!`) or we only treat a leading `! ` (bang-space) as the
  directive sigil — pick one and document it.
- **Interleaving with the run live view.** If a run is streaming in the same
  terminal, entering `pm chat -i` is a foreground takeover (like any REPL
  command); Item 2 is what keeps the PM visible *without* leaving the run view.
- **Item 2 duplicate suppression** across a reconnect / empty tick must not
  reprint the tail or lose the cursor (same guarantees as the F151 log tail).

## Testing

- **Item 1** (deterministic, injected `read_line`/`write`, mocked client): a
  scripted session — a question line hits `POST /pm-ask` and prints the reply; a
  `!directive` line hits `POST /interject` and prints the delivered
  confirmation; an applied-change reply prints the `pm changes` hint; `/exit`
  ends the loop; `--json`/non-interactive refuses without reading. Assert the
  underlying routes/guards are the same objects as the one-shot path (parity).
- **Item 2**: growing `/pm-chat` payloads across ticks → new `PM:` lines print
  once, in order, no reprints; a request-for-input surfaces a level-0 headline
  even at `-V quiet`; user turns are not echoed; piped output has no ANSI.
- **Item 3**: reuse the F151 log-tail test shape for `pm chat --watch`
  (append-once, quiet tick prints nothing, reset reprints, piped plain text);
  assert `pm` steering subs remain un-watchable.
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

- **A dedicated "PM question to the user" signal kind** (engine change to
  `attention.py`, whose kinds are today only `problem`/`alert`) so a PM request
  for input is a first-class, resolvable item rather than inferred from chat/
  attention. This is the clean long-term backing for Item 2's "needs you" nudge.
- **Server push / SSE** for PM turns (removes the poll cadence entirely) — a
  cross-cutting transport change well beyond this feature.
- **`?since=<cursor>` on `/pm-chat`** (bandwidth) — same follow-up shape as the
  team-log tail cursor noted in F151.
- **Multi-thread PM chat** (the route already takes `thread_id`).
- **Multi-line composer** in the interactive loop (paste a paragraph) — v1 is
  line-per-turn.
