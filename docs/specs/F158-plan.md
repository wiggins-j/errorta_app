# F158 — Implementation plan (CLI PM conversation)

Spec: [F158-cli-pm-conversation.md](F158-cli-pm-conversation.md).
This plan reflects a grounding pass (subagent review) over the code; where it
refines the spec, the **Δ** notes say how. All changes are **CLI-only** — no
engine/route change in v1 (Item 2b, the "needs you" headline, is explicitly a
follow-up because it needs the engine).

## Grounding corrections (already folded into the spec)

- **Δ P0 — client timeout is the one real code change.** `SidecarClient` uses a
  scalar 30s read timeout (`client.py:40` `_DEFAULT_TIMEOUT = 30.0`) and `pm ask`
  posts with no override (`pm.py:104-105`), but `pm_ask` waits **90s default /
  120s cap** (`coding.py:1747-1750`). Slow PM turns already die at 30s →
  `SidecarUnreachable` (`client.py:100-103`), so one-shot `pm ask` is *already*
  broken for slow turns. Fix: a per-call `timeout=` on `request`/`post_json`,
  ≈130s for pm-ask. Not "pure presentation" — it's a small client change.
- **Δ `watch_mode` is per-Command, not per-sub.** `run_watch` reads
  `command.watch_mode` (`watch.py:66`) and never sees the sub-verb; `pm` is one
  `Command` (`pm.py:199`) with a tail-able `chat` and snapshot `changes`. Need a
  `watch_mode_for(args)` hook on `Command` (default → static `watch_mode`), not a
  field flip on `pm.py`. `_run_stream`'s log hard-wire is at `watch.py:123`
  (import) / `:130` / `:143`.
- **Δ `pm-ask` has TWO non-answer branches.** `answered:false,
  error:"pm_unreachable"` (`coding.py:1763`) AND `answered:false,
  kind:"unconfigured"` with **no `error` key** (`coding.py:1719-1728`). Branch on
  `answered is False`, then message off `kind`/`error` — never key on `error`
  alone (KeyError risk).
- **Δ Item 2b needs the engine, so it's out of v1.** `pm_ask` writes only
  `kind` chat/error/unconfigured; `attention.KINDS == ("problem","alert")`
  (`attention.py:30`); a chat question raises no attention signal; and event
  visibility is a pure channel gate (`poller.py:252-256`) with no level-0
  headline primitive in the channel feed. The quiet "needs you" nudge therefore
  needs a new `needs_input` signal kind — a separate follow-up PR.
- **Δ Reuse points confirmed.** `wizard.run_wizard` already takes
  `read_line`/`write` (`wizard.py:116`) — the injectable pattern to copy;
  `_applied_refusal_lines` lives in `commands/pm.py:185` (not `render/pm.py`);
  bare `/pm` currently falls through to `registry.dispatch` at `repl.py:67-68`;
  `poller.DEFAULT_SOURCES` (`poller.py:47-64`) and `verbosity.CHANNEL_MIN_LEVEL`
  (`verbosity.py:41-51`) take a new `pm-chat` source / `pm` channel cleanly.

## Phase 0 — land the spec + this plan (no code)

Branch `feat/F158-cli-pm-conversation`; commit the (reviewed) spec + this plan.
Keeps the spec-first convention (F151/F157).

## Phase 1 — P0: per-call HTTP timeout (foundational, tiny)

Lands first and independently — it also repairs today's one-shot `pm ask`.

1. `client.py`: add an optional `timeout: float | None = None` to `request` (and
   the `get_json`/`post_json`/`put_json` wrappers), passing it into the httpx
   call when set; default behavior unchanged (still `_DEFAULT_TIMEOUT`).
2. `commands/pm.py`: the `_ask` POST passes `timeout=130.0` (120s server cap +
   margin). Add a module constant `_PM_ASK_TIMEOUT = 130.0` with a comment citing
   `coding.py:1747-1750`.

**Tests (`tests/cli/test_pm_conversation.py`, new):**
`test_pm_ask_uses_extended_timeout` — a mock transport that asserts the request
was issued with the ≈130s timeout (or a slow handler that would trip 30s but not
130s); `test_default_calls_keep_30s` — a read command still uses the default.

## Phase 2 — Item 1: interactive PM chat mode (the headline)

Depends on Phase 1 (a real conversation must survive a slow turn).

1. **Factor the reader.** Extract the prompt-read primitive from
   `repl.run_repl` into an injectable `read_line`/`write` pair (mirror
   `wizard.run_wizard`, `wizard.py:116`) so the loop is unit-testable headless.
2. **`_pm_interactive(client, ctx, *, read_line, write)`** in `commands/pm.py`:
   - On entry, render the transcript (`GET /pm-chat`, reuse the `pm chat`
     render).
   - Loop: read a line; classify by the documented grammar — bare → question
     (`_ask` path, `POST /pm-ask` with the Phase-1 timeout); leading `! ` →
     directive (interject path); a leading token in `{/exit,/changes,/help}` →
     meta; anything else starting with `/` or `!` → question. Print the PM reply
     (or, on `answered is False`, the busy/unconfigured message per the Δ) and
     any `_applied_refusal_lines` (`pm.py:185`) + the `pm changes` hint when
     changes applied.
   - `/exit`, Ctrl-D, Ctrl-C leave the loop; Ctrl-C during an in-flight ask
     aborts that turn only.
3. **Entry points.** `pm.py`: a `-i`/`--interactive` `Param`; when set (and TTY),
   `_call` runs `_pm_interactive` instead of the one-shot. Under `--json` /
   non-interactive, refuse with a clean CliError (mirror `wizard.py:112-115`).
   `repl.py handle_line`: route **bare** `/pm` (no sub) into the loop; `/pm chat`,
   `/pm ask …`, etc. dispatch as today.

**Tests:** scripted `read_line`/`write` + mock client — question→`/pm-ask`
reply; `!x`→`/interject` confirmation; applied-change reply prints the hint; both
`answered:false` branches survive without KeyError and keep looping; a `/etc/...`
question is sent, not treated as meta; `/exit` ends; `--json`/non-interactive
refuses without reading. Parity: assert the loop reuses the same route calls as
one-shot `pm ask`/`interject`.

## Phase 3 — Item 3: `pm chat --watch` tails (sub-aware stream)

Independent of Phase 2; can land in parallel.

1. **Sub-aware watch mode.** Add `watch_mode_for(self, args) -> str` to the
   registry `Command` (default returns the static `watch_mode`); `run_watch`
   calls `command.watch_mode_for(resolved_args)` instead of reading the field
   (`watch.py:66`). `pm`'s command returns `"stream"` iff the sub is `chat`.
2. **Generalize `_run_stream`.** Replace the hard `from .render.log import …`
   (`watch.py:123`) with per-`Command` hooks `stream_entries(payload) -> list`
   and `render_entries(entries) -> list[str]` (defaults = the log
   implementation). `pm` supplies `payload["thread"]` + `_render_chat` per-turn
   formatting. The `_entry_key` LCP diff (`watch.py`) already keys on
   `(at,role,member,kind,message)`, which pm-chat turns satisfy.

**Tests:** the F151 log-tail shapes retargeted at `pm chat --watch`
(append-once, quiet tick silent, reset reprints, piped plain text); **regression:
`pm changes --watch` still snapshots** (the sub-aware resolver); `pm ask --watch`
/ steering subs stay rejected (`_reject_watched_mutation`, `pm.py:45-51`);
`log --watch` byte-identical (shared path unchanged).

## Phase 4 — Item 2a: the `pm` live channel

Independent of Phases 2–3.

1. `poller.py`: add an append-mode `pm-chat` `Source` (path `GET /pm-chat`,
   channel `pm`, keyed like the log entries) to `DEFAULT_SOURCES`.
2. `verbosity.py`: add `"pm": Level.DEFAULT` to `CHANNEL_MIN_LEVEL`.
3. `runstream.py`: emit new PM turns (`role == "pm"`) as `PM: …` via the same
   LCP-diff helper; do not echo the user's own turns.

**Tests:** growing `/pm-chat` payloads across ticks → each PM turn prints once,
in order, no reprint; user turns not echoed; channel silent at `-V quiet`, shown
at `default`; piped output has no ANSI.

## Phase 5 — docs

- `docs/CLI.md`: a "Talking to the PM" subsection in Mid-run steering (question
  vs `!`directive, `pm chat -i`, in-loop `/exit`/`/changes`/`/help`); add the
  `pm` channel to the verbosity table (level 1); note `pm chat --watch` tails.
- `README.md`: steering snippet shows `errorta pm chat -i`.
- Keep `--help` strings in sync.

## Ordering & rollout

**Phase 1 → 2** are a chain (the loop needs the timeout). **Phases 3 and 4** are
independent and can land in any order. Suggested: **one PR** for Phases 1–4
(coherent feature) with a commit per phase for review legibility; Phase 0 is the
spec/plan commit. Item 2b (headline + `needs_input` kind) is a **separate later
PR** — it touches the engine and has its own review surface. Gate: full CLI
suite (`567`+ currently) + the new `test_pm_conversation.py`; `ruff`;
frozen-binary smoke of `pm chat -i` against a live sidecar.

## Risk register

- **P0 timeout regression (highest-value fix, lowest risk).** A per-call arg with
  an unchanged default can't affect other calls; the risk is *forgetting* it and
  shipping a chat loop that dies at 30s. The Phase-1 test is the gate.
- **Sub-aware `watch_mode` touches shared `run_watch`.** A wrong refactor could
  change snapshot behavior for every watched read. Mitigation: `watch_mode_for`
  defaults to the current field, so all existing commands are byte-identical; the
  `pm changes --watch` regression test locks the one new branch.
- **Interactive loop blocking a run's live view.** Entering `pm chat -i` is a
  foreground takeover; Item 2a is the non-blocking way to see the PM during a
  run. No shared-state hazard (each is its own poll loop).
- **`pm-ask` agency.** A PM reply can apply config changes mid-chat; the loop
  surfaces `applied`/`refusals` + a `pm changes` hint so nothing is silent, and
  every change remains revertible via `pm decline`. No new authority is granted.
- **Scope creep into 2b.** The tempting "PM needs you" headline has no v1
  trigger; keep it out of the v1 PR (its own follow-up) so v1 stays engine-free
  and shippable.
