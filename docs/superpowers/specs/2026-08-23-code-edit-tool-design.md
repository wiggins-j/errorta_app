# code_edit — anchored find/replace for coding-team dev turns

**Date:** 2026-08-23
**Status:** Approved (operator work order, 2026-08-23)
**Scope:** `python/errorta_council/coding/` (Coding Mode only — NOT the legacy
council tool catalog in `errorta_tools/catalog.py` / `errorta_council/schema.py`)

## Problem

Coding Mode devs have exactly one write tool: `code_write`, which replaces a
whole file. Live incident (senditai-ng task `t-e75a9c4d5a2e`, 2026-08-24): an
opus dev correctly diagnosed a fix in the 1600-line `run_live.py`, but its
whole-file rewrite collapsed/truncated under the turn's output budget and
`write_guard.classify_destructive_write` blocked it (`destructive_write_blocked`).
The block was RIGHT — the write would have gutted the file — but the net effect
is that **any fix to a large file is structurally impossible**: the only
expressible write is one the model cannot reliably produce at that size.

## Solution

Add a second dev tool, `code_edit`: anchored find/replace with Claude Code
`Edit`-tool semantics. The model supplies the exact text to replace
(`old_string`) and its replacement (`new_string`); the harness locates the
anchor in the current file and splices. Output cost is proportional to the
CHANGE, not the file.

Explicitly NOT a line-number patch format — models fumble line arithmetic;
they are reliable at quoting exact text they can see (the dev prompt already
inlines the worktree readback, so anchors are available).

## Tool contract

```json
{"tool": "code_edit", "args": {
  "path": "rel/path/in/worktree",
  "old_string": "<exact existing text, must match exactly once>",
  "new_string": "<replacement text>",
  "replace_all": false
}}
```

- `path` — worktree-relative; same traversal guard as `code_write`
  (`resolve_workspace_path`).
- `old_string` — matched EXACTLY (byte-for-byte after the file's lenient UTF-8
  decode; no whitespace normalization, no regex). Must be non-empty and match
  exactly once, unless `replace_all`.
- `new_string` — must differ from `old_string`.
- `replace_all` — optional, default false. With it, `old_string` may match any
  number of times ≥ 1 and every occurrence is replaced. Included because it is
  part of the Edit-tool vocabulary strong models already know, and because
  without it a rename-style edit has no expressible escape from
  `edit_not_unique`.
- Text only. There is no `content_base64` arm; binary assets stay on
  `code_write`.
- `code_edit` never creates a file. New files are `code_write`'s job.

### Failure taxonomy

Each failure is a **failed tool event** (same channel as `code_write`
failures: recorded via `record_tool_event`, surfaced as a `write_failed`
decision, carried into the F136 unproductive/escalation ladder when no write
in the turn succeeds). Error strings are prefixed with a stable code so tests
and the carry-forward prompt hint can match on prefix:

| code | condition | detail includes |
|---|---|---|
| `edit_invalid_args` | `old_string`/`new_string` absent or not strings (JSON-type check at the executor, before the workspace is touched) | — |
| `edit_target_missing` | file does not exist in the worktree | "code_edit cannot create a file; use code_write" |
| `edit_target_binary` | existing file is binary (NUL-byte heuristic, same as `write_file`) | — |
| `edit_empty_old_string` | `old_string` empty/blank | — |
| `edit_no_change` | `old_string == new_string` | — |
| `edit_no_match` | 0 occurrences | reminder that matching is exact (whitespace included) |
| `edit_not_unique` | N > 1 occurrences without `replace_all` | the count N; advice to enlarge the anchor with surrounding lines or set `replace_all` |

Path traversal and destructive-write blocking reuse the existing codes
(`resolve_workspace_path` error; `destructive_write_blocked`).

### Write-guard interaction (required behavior)

The spliced result is a full old→new file pair, so the F140 guard
(`classify_destructive_write(old_full, new_full)`) applies to `code_edit`
exactly as to `code_write` — an edit whose `new_string` drops a huge unique
block from a large file still classifies as `gutted`/`truncation` and is
blocked. This falls out of the architecture (below) rather than being
re-implemented.

## Architecture

Follows the existing layering (pure module → workspace → controller → prompt),
one new file plus wiring:

1. **`edit_apply.py` (new, pure)** — mirrors `write_guard.py`'s style:
   dependency-free, unit-testable without git or a workspace.
   `apply_code_edit(old_content, old_string, new_string, replace_all=False) -> str`
   returns the new full content or raises `EditApplyError(code, detail)` with
   the taxonomy codes `edit_empty_old_string` / `edit_no_change` /
   `edit_no_match` / `edit_not_unique`. Occurrence counting uses
   non-overlapping `str.count` semantics (matching `str.replace`).

2. **`workspace.py`** — `CodingWorkspace.edit_file(rel_path, old_string,
   new_string, *, replace_all=False, task_id, summary="") -> str` (new HEAD
   sha). Resolves the path in the task worktree (existing
   `resolve_workspace_path` safety), rejects a missing file
   (`edit_target_missing`) or binary file (`edit_target_binary`, NUL
   heuristic shared with `write_file`), decodes leniently
   (`utf-8, errors="replace"` — same as the guard path), applies
   `apply_code_edit`, then **delegates the final content to the existing
   `write_file`** so the F140 guard, no-op-commit suppression (F139),
   provenance upsert, and HEAD return are shared, not duplicated. Failures
   raise `CodingWorkspaceError` carrying the taxonomy code as the message
   prefix.

3. **`turn_controller.py`** — `_ROLE_TOOLS[DEV] = ("code_write", "code_edit")`.
   `execute_dev_turn` dispatches per tool: the `code_edit` branch type-checks
   args (strings; bool), calls `workspace.edit_file`, and records
   succeeded/failed tool events with `tool="code_edit"`. `_safe_intent`
   records `path`, `old_bytes`, `new_bytes`, `replace_all` — never the full
   strings (ledger hygiene, same reasoning as `content_bytes`).
   `tool_catalog_text` needs no change: the tools line derives from
   `allowed_tools_for_role` (the Spec-17 invariant test keeps it honest).

4. **Prompt (`runner._dev_prompt_segments`)** — teach the vocabulary:
   - the readback preamble ("code_write replaces the whole file so include all
     of it") gains: for a targeted change to an existing file, prefer
     `code_edit` instead of re-emitting the file;
   - the envelope instruction shows both tools with a `code_edit` example and
     the rule of thumb: `code_edit` for modifying existing files (REQUIRED
     habit for large files — a whole-file re-emit of a large file gets
     truncated and blocked), `code_write` for new files, full rewrites, and
     binary assets;
   - the prompt golden (`test_prompt_segments_golden.py`) moves with it,
     byte-for-byte, per that test's own contract.

5. **`schemas.py`** — no new envelope/intent models (`ToolCall` is
   deliberately generic; arg validation lives at execution where failures are
   recorded as events, not parse rejections). Add a
   `("dev", "tool_plan_edit")` entry to `_MINIMAL_INTENT_EXAMPLES` so the
   corrective-prompt table teaches the shape and the Spec-25 expressibility
   test round-trips it.

6. **Cosmetic couplings** — runner.py's `write_missing` rationale string
   ("no code_write tool event") generalized to name both write tools;
   capabilities.py's `CLOSURE_TABLE` comment citing
   `_ROLE_TOOLS[DEV] == ("code_write",)` updated.

## Testing

- `tests/coding/test_edit_apply.py` (new): pure-function matrix — unique match
  splice, replace_all multi-splice, every failure code, exact-whitespace
  sensitivity, non-overlapping count semantics, unicode content.
- `tests/coding/test_turn_controller.py` (extend): `code_edit` allowed for
  DEV and only DEV; dispatch to `workspace.edit_file`; failed-event recording
  with taxonomy prefixes; `_safe_intent` shape; a mixed
  `code_write`+`code_edit` turn; two sequential `code_edit` calls to the same
  file compose (second sees first's result).
- `tests/coding/test_coding_workspace.py` (extend): happy path commits and
  returns HEAD + provenance; `edit_target_missing`; `edit_target_binary`;
  traversal rejection; **guard interaction** — an edit that guts a large file
  raises `destructive_write_blocked`; a no-net-change turn behaves per F139.
- Existing suites updated where they lock the old single-tool reality:
  prompt golden, Spec-17 catalog test (should pass unchanged — verify),
  Spec-25 expressibility (auto-enumerates the example table), GL03/closure
  tests if any assert the DEV tools tuple.

## Non-goals

- No `code_edit` for reviewer/tester/designer (tool discipline: only DEV
  writes).
- No registration in the legacy council tool catalog.
- No multi-edit array argument — a turn already carries multiple `tool_calls`.
- No fuzzy/whitespace-insensitive matching, no line-number addressing.
- No change to the F140 guard thresholds.
