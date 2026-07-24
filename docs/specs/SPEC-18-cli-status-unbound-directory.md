# Spec 18 — `errorta status` from an unbound directory

**Source:** `docs/coding/RUN_ANALYSIS_GRAVITY_GOLF_2026-07-24.md` §6 **S7 (P2)**
**Target version:** v0.1 (CLI)
**Status:** proposed
**Owner:** wiggins-j

---

## Problem

With a live run in progress, `errorta status` run from any directory that is not
bound to that project prints sidecar health and then stops:

```
project: (none bound to this directory)
```

The sidecar knows exactly which projects exist and which one is running — the
command just never asks. From the operator's seat the tool looks like it has
nothing, at the exact moment the most interesting thing in the system is
happening. (Observing the gravity-golf run required reading ledger files by
hand.)

Mechanically: `commands/status.py:_call` fetches `/healthz`, and fetches the run
only when `ctx.project_id` is set; `render_status` returns early at
`render/status.py:54-57` when `project_id` is absent. Project binding comes from
`config.resolve_project_id` (`config.py:182-202`) — a `.errorta-project` pointer
in the cwd or an ancestor, else a cwd inside some project's `repo_path` /
`delivery_root`. A run driven from a home directory, a scratch shell, or a second
terminal resolves to nothing, which is correct and unhelpful.

Everything needed is already there: `GET /coding/projects`
(`routes/coding.py:509-512`) returns every project with a derived
`list_status` / `list_status_reason` computed from the reconciled run state,
liveness, stop reason, and open blocking attention signals
(`_project_list_out`, `routes/coding.py:514-545`). The `projects` command
(`commands/project.py:394-398`) already renders it.

## Goals

- An unbound `errorta status` reports **what the sidecar is actually doing** —
  which projects exist, which are running, which need attention.
- It says **how to target one**, with commands the operator can run as printed.
- The bound path is completely unchanged.

## Non-goals

- Not auto-binding, auto-selecting, or mutating any state. `status` is a read;
  binding stays an explicit `errorta open <id>`.
- Not duplicating `errorta projects`. Status shows a **bounded, run-focused**
  slice — running first, then attention, then the rest, capped — and points at
  `projects` for the full list.
- No new route. `GET /coding/projects` is sufficient.

---

## Item 1 — Fetch the project list when nothing is bound

**Design.** In `commands/status.py:_call`, when `ctx.project_id` is falsy, also
call `client.get_json("/coding/projects")` and return it under a `projects` key.
The bound branch is untouched — in particular it keeps making exactly one run
call, preserving the deliberate sole-ownership reasoning documented at
`commands/status.py:20-27` (that route is side-effecting; it runs recovery /
reconcile).

`GET /coding/projects` is a plain read with no origin guard
(`routes/coding.py:509`), so no `_mutate.guard_sole_owner` gate applies.

Guarded: if the call fails, fall back to today's output rather than turning a
health check into an error. `status` is the command an operator reaches for when
things are broken; it must not itself break.

**Acceptance.** Unbound `status` issues `/healthz` + `/coding/projects` and no
other call. Bound `status` issues `/healthz` + the run route, exactly as today.
A failing project-list call degrades to today's output.

## Item 2 — Render an actionable unbound view

**Design.** Replace the early return at `render/status.py:54-57` with a block
that keeps the existing "no project bound to this directory" line (same wording
as `NO_PROJECT_MSG`, `render/__init__.py:56-60`, which already names `new` /
`import` / `wizard` / `open`) and then adds:

- **running projects first**, one line each: id, `list_status`, and the
  `list_status_reason` when it explains something (a stop reason, a blocking
  attention signal);
- then projects with blocking attention, then the rest, **capped** (default 5,
  `--all` / `--json` for everything) with an `(+N more — errorta projects)`
  tail so the view stays a status view;
- a closing hint naming the exact next commands:

  ```
  errorta open <id>      bind this directory
  errorta watch          live dashboard for the bound project
  ```

Style follows the existing conventions: `_STATUS_STYLE` (`render/status.py:17-23`)
for run states and `_TERMINAL_BAD` (`:26-30`) for genuinely bad stop reasons, so
   — **and fix that set while here (Δ review):** it is missing
   `gate_not_improving`, `planning_churn` and `dispatch_wedged`, which Specs
   04/07/10 added without updating it. Its only consumer is the stop-reason
   styling at `:68`, this spec's own surface, so the correction belongs here
   rather than in [Spec 16](SPEC-16-revise-chain-circuit-breaker.md) — which then
   merely appends its own reason and carries no ordering constraint against this
   spec. Continuing:
a failed run reads as failed here too. `--json` returns the raw payload
unchanged (`make_render`, `commands/_base.py`), so scripts get the full list.

**Acceptance.** With one running project and two idle, unbound `status` lists the
running one first with its status, caps at 5 with a `+N more` tail beyond that,
and prints the two hint lines. With **no** projects at all, it prints the
existing no-project message and nothing else — an empty table would be noise. The
bound rendering is byte-identical to today.

---

## Implementation notes

- **`commands/status.py`** — the unbound branch in `_call` (`:19-31`); no
  registry/param change (an optional `--all` is the only new param, and only if
  the cap proves annoying in practice).
- **`render/status.py`** — replace the early return (`:54-57`); reuse
  `_STATUS_STYLE` / `_TERMINAL_BAD` (`:17-30`) and `muted` / `render`
  (`render/__init__.py`).
- **Possible reuse:** `render/project.py` already renders the project list for
  the `projects` command; factor the row formatter rather than writing a second
  one, so the two views cannot drift on status wording.
- Works identically in both front-ends because both dispatch through the shared
  registry (F147 §5.2) — no REPL-specific code.

## Edge cases

- **No sidecar / unreachable**: unchanged — the `/healthz` failure path already
  governs, and the project-list call is guarded on top of it.
- **Many projects** (dozens): capped list + `+N more` tail; `--json` is the
  complete answer for scripting.
- **A project whose ledger is unreadable**: `_project_list_out` already swallows
  `LedgerError` and returns empty state (`routes/coding.py:531-535`), so the row
  renders with an unknown status rather than failing the command.
- **A remote/residency-restricted sidecar**: `GET /coding/projects` carries no
  `refuse_local_dataplane_if_remote` guard, so it behaves like any other read.
- **The cwd is inside a project's delivery root but the pointer is missing**:
  resolution already covers it (`config.py:194-202`), so this path is genuinely
  "nothing bound", not a resolution bug being papered over.

## Testing

- **Item 1**: unbound `_call` issues both requests and no run call; bound `_call`
  is unchanged (assert the exact request set); a raising project-list call
  degrades to the health-only payload.
- **Item 2**: rendering with (a) no projects → existing message only, (b) one
  running + two idle → running first, hints present, (c) 8 projects → 5 rows +
  `+3 more`, (d) a project with a `_TERMINAL_BAD` stop reason → bad style,
  including each of the three reasons this spec backfills,
  (e) bound payload → byte-identical to the current golden.
- `--json` returns the full list unfiltered.
- CLI test suite + `ruff`.

## Documentation

- `docs/CLI.md` (§9 run status): `status` from an unbound directory lists active
  projects and how to target one; `--json` returns the full list.

## Out of scope / follow-ups

- Auto-binding to the single obvious project when exactly one exists (tempting,
  and a surprising mutation from a read command).
- A `--project <id>` global flag to target any project from anywhere without
  binding — genuinely useful, and a larger change to the registry/context model
  than this spec.
- Showing per-project progress counters (iteration, turns) in the unbound list;
  that is `errorta watch`'s job.
