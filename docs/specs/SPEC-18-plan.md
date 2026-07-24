# Spec 18 — Implementation plan (`errorta status` from an unbound directory)

Spec: [SPEC-18-cli-status-unbound-directory.md](SPEC-18-cli-status-unbound-directory.md).

**Owner:** Engineer B · **Branch:** `feat/spec-18-status-unbound`
**Base:** `chore/spec-12-18-prep` (merged) · **PR into:** `main`
**Land after** [Spec 16](SPEC-16-plan.md) — both edit
`errorta_cli/render/status.py` (16 touches `_TERMINAL_BAD` at `:26-30`, this
spec the unbound early return at `:54-57`). Otherwise fully independent: CLI
only, no engine change, no route change, no dependency on Engineer A.

The smallest spec in the batch. It exists because observing the gravity-golf run
required reading ledger files by hand while `errorta status` printed
`project: (none bound to this directory)`.

## Phase 0 — spec + plan (no code)

Branch off the merged prep PR; commit the spec + this plan.

## Phase 1 — fetch the project list when nothing is bound

`commands/status.py:_call` (`:19-31`): when `ctx.project_id` is falsy, also call
`client.get_json("/coding/projects")` and return it under a `projects` key.

- The **bound** branch is untouched — in particular it keeps making exactly one
  run call, preserving the sole-ownership reasoning documented at
  `commands/status.py:20-27` (that route is side-effecting: it runs
  recovery/reconcile).
- `GET /coding/projects` (`routes/coding.py:509-512`) is a plain read with no
  origin guard, so no `_mutate.guard_sole_owner` gate applies.
- **Guarded**: a failing project-list call falls back to today's health-only
  payload. `status` is what an operator reaches for when things are broken; it
  must not itself break.

**Tests** (`tests/cli/test_status_unbound.py`, new): unbound `_call` issues
`/healthz` + `/coding/projects` and **no** run call (assert the exact request
set); bound `_call` is unchanged; a raising project-list call degrades to the
health-only payload.

## Phase 2 — render an actionable unbound view

Replace the early return at `render/status.py:54-57`:

1. Keep the existing "no project bound to this directory" line — same wording as
   `NO_PROJECT_MSG` (`render/__init__.py:56-60`), which already names `new` /
   `import` / `wizard` / `open`.
2. Then: **running projects first** (id, `list_status`, and
   `list_status_reason` when it explains something — a stop reason, a blocking
   attention signal), then projects with blocking attention, then the rest.
   **Capped at 5**, with a `(+N more — errorta projects)` tail so this stays a
   status view rather than a second `projects` command.
3. Close with the exact next commands:

   ```
   errorta open <id>      bind this directory
   errorta watch          live dashboard for the bound project
   ```

4. Style via the existing `_STATUS_STYLE` (`:17-23`) and `_TERMINAL_BAD`
   (`:26-30`, freshly corrected by Spec 16) so a failed run reads as failed here
   too.
5. **With no projects at all**, print the existing message and nothing else — an
   empty table is noise.

`--json` returns the raw payload unchanged (via `make_render`,
`commands/_base.py`), so scripts get the full list.

**Reuse, don't duplicate:** `render/project.py` already formats these rows for
the `projects` command — factor the row formatter rather than writing a second
one, or the two views drift on status wording.

**Tests.** No projects → existing message only; one running + two idle → running
first, both hint lines present; 8 projects → 5 rows + `+3 more`; a project with a
`_TERMINAL_BAD` stop reason → failure style; bound payload → byte-identical to
the current golden; `--json` → the full unfiltered list.

## Phase 3 — docs

`docs/CLI.md` (§9 run status): `status` from an unbound directory lists active
projects and how to target one; `--json` returns the full list.

## Definition of done

CLI test suite + `ruff` green. The bound rendering is byte-identical to `main`
(the regression lock). Works identically in shell and REPL — both dispatch
through the shared registry (F147 §5.2), so no REPL-specific code should appear
in the diff.
