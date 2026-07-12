# F151 — CLI run ergonomics: `stop` alias, `run --autonomous`, live `log --watch`

**Target version:** v0.1 (CLI)
**Status:** proposed
**Owner:** wiggins-j

> Feature number is provisional — confirm against the F-registry before merge.

---

## Problem

Three papercuts surfaced driving a real run (the reddit-clone demo) from the CLI:

1. **`errorta stop` doesn't exist.** Typing `stop` (the obvious verb) errors with
   *"No such command 'stop'. Did you mean 'setup'?"*. The command is `cancel`, but
   nothing points there and `stop` is what everyone reaches for.
2. **Going autonomous is a hidden two-step.** To run without stopping at every
   checkpoint you must know the incantation `errorta setup --confirm
   --checkpoint-cadence off --yes` and then `run`/`continue`. The desktop app has a
   one-click "autonomous"; the CLI buries it.
3. **`errorta log --watch` doesn't tail cleanly.** It **full-repaints** the entire
   log every poll tick (`\x1b[H\x1b[2J` then re-render). In practice the frames
   **stack** (the same block reprinted with blank gaps) and it reads as "not adding
   new events" — the wrong model for a growing event stream.

## Goals

- `errorta stop` works as an alias of `errorta cancel`.
- `errorta run --autonomous` (and `continue`/`resume --autonomous`) sets
  `checkpoint_cadence=off` for you — one flag, matches the UI.
- `errorta log --watch` behaves like `tail -f`: new events **append** as they
  arrive; no full-screen repaint, no stacking.

## Non-goals

- Changing the engine's autonomy/scheduler semantics (`checkpoint_cadence=off`
  already exists and works — F151 only surfaces it).
- Reworking snapshot watches (`status`/`tasks`/`board --watch`) — those are
  point-in-time views and stay full-redraw.
- New governance modes.

---

## Item 1 — `stop` as an alias for `cancel`

`cancel` requests cancellation at the next turn boundary (`POST /run/cancel`).
Add `stop` as a true alias so `errorta stop` == `errorta cancel` (same call,
confirm gate, `--yes`, render).

**Design.** Add an `aliases: tuple[str, ...] = ()` field to the registry
`Command`. `register()` records aliases in a **separate `_ALIASES: dict[str,str]`
map** (alias → canonical name) — NOT a second entry in `_REGISTRY`, so
`all_commands()` / `names()` stay canonical and the parity tests that loop every
command (`tests/cli/test_registry_parity.py`) don't double-dispatch a mutating
command. `get()` falls back to `_ALIASES`. The argv/Typer exposure
(`_add_argv_command` / `_register_argv_commands`) must **explicitly register an
extra Typer command per alias** (Typer resolves subcommands by registered name,
so `errorta stop` 404s otherwise) whose handler dispatches under the canonical
name. Add aliases to the REPL `WordCompleter` for `/stop` autocomplete. Give
`cancel` `aliases=("stop",)`. No conflict: `errorta sidecar stop` /
`errorta runtime stop` are namespaced sub-actions, not top-level.

**Acceptance.** `errorta stop` and `errorta stop --yes` behave exactly like
`cancel`; `--help` lists `stop` (or notes it as an alias); slash `/stop` resolves
too (shared registry).

## Item 2 — `run --autonomous` (and `continue`/`resume --autonomous`)

A `--autonomous` flag that runs the council to completion without stopping at
checkpoints — i.e. sets the autonomy policy's `checkpoint_cadence=off` (the loop
still stops on Definition-of-Done, budget, hard blocker, `member_unhealthy`, or
`cancel`).

**Design.** `--autonomous` is CLI sugar over the existing policy setter: before
issuing the start/continue call, the command applies `checkpoint_cadence=off` via
the run-setup confirm (`POST /run-setup/confirm` with `{checkpoint_cadence:
"off"}` — the route **merges** the policy, so it disturbs nothing else). Then it
proceeds with the normal `/run` (or `/continue` / `/resume`). No server change:
`/run` takes no policy today, and confirm-then-start reuses proven machinery.

- On `run`: set policy → start. Combine cleanly with `--members`/draft team.
- On `continue` / `resume`: set policy → the loop re-reads it on resume (the
  policy is "editable mid-run"), so a run stopped at a checkpoint continues
  autonomously. This is the fix for "I already started; don't stop again."
- Optional parity: also accept `--checkpoint-cadence <off|per_milestone|
  every_n_tasks|on_merge_ready>` on `run`/`continue` for the non-binary cases;
  `--autonomous` is the headline shortcut for `off`.

The autonomous confirm sends **policy fields only** (never the team) — verified
against `confirm_run_setup` (coding.py:2778): step 2 does load→merge→save of the
autonomy policy (preserves other knobs + governance), `set_run_config` is guarded
`if members:` so an empty-members confirm leaves the applied team intact, and
`_set_run_setup_confirmed(True)` is unconditional so the confirm itself satisfies
the readiness gate. (Do NOT use `PUT /autonomy` — `policy_from_dict` fills every
omitted field from defaults, silently clobbering `max_iterations` etc.)

For `continue`/`resume`, the loop reads the policy once per worker start
(`runner.run(load_policy(store), …)`), and continue/resume spawns a fresh worker
— so setting the policy then continuing works exactly as claimed.

**Acceptance.** After `errorta run --autonomous --yes`, the resolved setup shows
`checkpoint_cadence: off`, no `checkpoint` stop is emitted; runs until
DoD/budget/blocker/cancel. `errorta continue --autonomous` on a checkpoint-stopped
run resumes without re-stopping. New behavior to test: `run --autonomous` on a
project with **no team assembled** now passes the readiness gate (the confirm sets
it) and fails later at the `/run` "no members" 400 — a different, still-clean
error surface than today's setup-required 409.

## Item 3 — `log --watch` as a real tail

`log` is a **growing event stream**, not a snapshot, so it should tail, not
repaint. Today `watch.run_watch` re-dispatches the full command and clears the
screen each tick (`watch.py:_draw` / `_CLEAR_SCREEN`), which repaints everything
and — when the clear doesn't take cleanly — stacks frames.

**Design.** Give the watch loop two modes:

- **snapshot** (current behavior) — `status` / `tasks` / `board` / most reads:
  full re-render + in-place clear each tick. Unchanged.
- **stream/tail** — `log` (and candidates `decisions` / `turns`): fetch the log
  each tick, print **only the new events**, appended below (no screen clear).
  Behaves like `tail -f`: old lines scroll naturally, new events stream in,
  nothing repaints.

A command declares its watch mode (e.g. `watch_mode = "stream" | "snapshot"` on
the `Command`, default `snapshot`); `log` opts into `stream`.

**No stable per-event key exists (the crux).** The `team-log` payload
(`coding.py:1428` → `build_team_log`, `team_log.py`) is a **derived, re-sorted**
view: entries are `{at, role, member, kind, message}` with **no id / seq**, `at`
is **non-unique** (e.g. `context_request`/`context_delivered` share a timestamp),
the list is **rebuilt and re-sorted by `at` every call** (so a late-arriving
earlier-timestamped source event can insert mid-list), and some entries **mutate
in place** across ticks (an approval renders "requested…" then later "approved…").
So an index- or timestamp-keyed diff is **unsound**.

**v1 approach — content longest-common-prefix (LCP) diff, CLI-only.** Keep the
previous rendered-entry list; each tick, compute the LCP against the new list
(compare per-entry content after applying the same `--role/--member/--grep`
filter). Print the entries after the LCP. If the LCP is **shorter** than what was
shown (a mid-list insertion, mutation, or log reset diverged the prefix), reprint
from the divergence point. Live runs are append-mostly at the tail, so this is a
clean tail in practice and degrades to an occasional reprint — never a wrong or
dropped event.

**Render refactor (required).** `render_log` (`render/log.py`) renders the whole
list as one block; the stream path can't reuse that string. Factor out a
per-entry renderer (`_render_entry(entry) -> Text`) used by both the block
renderer and the tail. The loop reads the structured `payload["entries"]` (already
captured but discarded at `watch.py:69`) and reuses `render.log._filter`.

The clean long-term fix — a server-side monotonic `seq` stamped per entry — is a
**recommended follow-up**, not v1 (it needs an engine change to `build_team_log`);
the LCP diff makes v1 correct-enough without touching the server.

**Acceptance.** `errorta log --watch` on a live run **appends** new events as they
occur, never reprints the existing log, and never stacks duplicate frames; on a
quiet run it simply waits (no repaint churn). Piped (`| tee`) stays plain text.
`Ctrl-C` exits cleanly. `--role`/`--member`/`--grep` filters still apply to the
streamed events.

---

## Implementation notes

- **Item 1** — `registry.Command.aliases`; `register()` + `get()` + the argv
  exposure honor it; `cancel` gets `("stop",)`. ~1 small registry change + 1 line.
- **Item 2** — `_run_call` / `_continue_call` / `_resume_call` in
  `commands/runctl.py`: a shared helper that, when `--autonomous` (or an explicit
  `--checkpoint-cadence`) is set, POSTs the policy to `run-setup/confirm` before
  the start/continue call. Add the `Param`s. Reuse `_CONFIRM_FIELDS` coercion.
- **Item 3** — factor `_render_entry` out of `render_log`; `watch.run_watch` gains
  a `stream` mode that consumes `payload["entries"]` (available at `watch.py:69`),
  applies `render.log._filter`, LCP-diffs against the prior list, and appends the
  suffix (no `_CLEAR_SCREEN`). `commands/log.py` sets `watch_mode="stream"`; the
  snapshot path stays byte-identical for everything else. Also verify
  `stream.isatty()` in the **frozen binary** — if a wrapped stdout reads
  non-TTY, today's snapshot clear is skipped and the log appends every tick
  (a second, frozen-only cause of the observed stacking); the tail removes this
  failure mode regardless.
- **cli.spec** — no new modules; all changes live in already-bundled files.
- **No server change required** for v1: confirm-merge for the policy (Item 2) and
  the content-LCP tail (Item 3). The per-entry `seq` (correct tail) and `?since=`
  (bandwidth) on `team-log` are follow-ups.

## Edge cases

- `stop` while no run is active → same "nothing to cancel" behavior as `cancel`.
- `run --autonomous` when providers are down → the pre-confirm still applies the
  policy; the run's own preflight surfaces the provider problem (unchanged).
- `continue --autonomous` when the run already finished (DoD) → no-op continue
  message (unchanged), policy harmlessly set.
- `log --watch` stream: first tick prints the existing tail (a bounded backlog,
  not the entire history if huge — decide a sane initial window, e.g. last N),
  then only deltas. Reconnect/empty-payload ticks must not reprint or lose the
  cursor. Clock skew / equal timestamps → tie-break on event index so nothing is
  dropped or duplicated.
- A run that resets the log (new run in the same project) → detect the cursor
  going backwards and reprint from the new start.

## Testing

- **Item 1**: `errorta stop`/`/stop` resolve to the cancel command object; dispatch
  hits `POST /run/cancel`; `--yes` gate parity.
- **Item 2**: `run --autonomous` issues the policy confirm (`checkpoint_cadence:
  off`) before `/run`; `continue --autonomous` sets policy before `/continue`;
  resolved setup reads `off`; `--checkpoint-cadence per_milestone` passthrough
  works; no `--autonomous` → policy untouched.
- **Item 3** (deterministic, mocked client): a sequence of growing `team-log`
  payloads across ticks → the tail prints each new event **once**, in order, with
  no repaint and no duplicates; a quiet tick prints nothing; a shrunk/reset log
  reprints from the new start; filters apply to streamed events; piped output has
  no ANSI.
- Full CLI suite + `ruff`; frozen-binary smoke test of all three.

## Documentation

- `docs/CLI.md`: `stop` in the run-control table (as a `cancel` alias);
  `--autonomous` on `run`/`continue`/`resume` with a one-line "no checkpoint
  stops"; a note that `log --watch` tails live. Update the mid-run steering guide.
- `README.md`: the quickstart's run line can show `errorta run --autonomous --yes`
  for the hands-off demo.
- Keep `--help` usage strings in sync.

## Out of scope / follow-ups

- `?since=<cursor>` on the `team-log` route (bandwidth optimization for the tail).
- Streaming-watch for `decisions` / `turns` (same mechanism; ship after `log`).
- A persisted per-project "autonomous by default" preference.
