# Spec 17 — Implementation plan (prompt / tool-catalog coherence)

Spec: [SPEC-17-prompt-tool-catalog-coherence.md](SPEC-17-prompt-tool-catalog-coherence.md).

**Owner:** Engineer B · **Branch:** `feat/spec-17-tool-catalog-coherence`
**Base:** `chore/spec-12-18-prep` (merged) · **PR into:** `main`
**Land first of Engineer B's four** — it settles the `tool_guidance` segment
shape that Specs 15 and (on Engineer A's side) 12 both build around. No
dependency on Engineer A.

The spec's **Δ review** notes that shape this plan:

- Item 3's stated delivery path does not exist: a `tool_not_allowed` failure
  happens **after** a successful parse, so it never reaches
  `_corrective_turn_prompt` (`runner.py:541`, gated on `_RETRYABLE_TURN_ERRORS`,
  `:515-519`). Better error text alone changes nothing the model sees — Phase 3
  adds a real carry-forward.
- The `dev_repo_read` default has a **fourth** disagreeing statement
  (`autonomy.py:230-233`). Its **value** is decided in the prep PR (P0.3), not
  here; this branch does the reconciliation.

**This branch owns every `tool_guidance` segment in the batch.** Do not touch
`gate_output` segments — those are Spec 12's (Engineer A).

## Phase 0 — spec + plan (no code)

Branch off the merged prep PR; commit the spec + this plan.

## Phase 1 — capability-aware tool catalog

1. Widen `tool_catalog_text` (`turn_controller.py:69-71`):

   ```
   tool_catalog_text(role, *, repo_read: bool = False, gate: bool = False) -> str
   ```

   Defaults preserve today's string exactly, so an un-updated caller is a no-op.
   It renders: the executed errorta tools (source of truth stays
   `allowed_tools_for_role`, `:35`); **when `repo_read`** the CLI-native read
   tools **by name** — `Read`, `Grep`, `Glob` — stated as available in the cwd,
   read-only, and **used directly mid-turn, not emitted as errorta tool calls**
   (the envelope carries only `code_write`; this distinction is precisely what
   today's framing omits); **when not**, the typed `context_request` dev intent
   (`schemas.py:167-188`), fully implemented at `runner.py:3285-3288` and never
   mentioned in any prompt; and always an explicit **negative** — no
   execute/run/shell tool exists for this role, do not plan one.
2. `_ROLE_TOOLS` (`:27-32`) and its F087-14 WS-3 rationale (`:20-26`) are
   untouched — this extends that discipline, it does not amend it.

**Tests** (`test_spec17_tool_catalog.py`, new — table-driven): role ×
`repo_read` × `gate`; every variant contains the no-execute-tool sentence; the
errorta-tool list is exactly `allowed_tools_for_role(role)`; the default-args
rendering is byte-identical to `main`'s string.

## Phase 2 — wire it into all four prompts

- `_dev_prompt_segments` (`runner.py:1607-1609`) — pass the live flags.
- `_review_pr_prompt_segments` (`runner.py:1713-1775`) — **new** segment (none
  today), including the "cite a file in every blocking finding; `file:line` in
  the body is better" rule that makes
  [Spec 14](SPEC-14-grounded-reviewer.md) Phase 3's `cited` flag satisfiable.
  Coordinate that wording with Engineer A.
- `_test_prompt_segments` (`runner.py:2077-2116`) — **new** segment.
- PM capability block: rendered by [Spec 15](SPEC-15-plan.md) Phase 4 from
  `capability_manifest`, which consumes this renderer.

**Resolve `repo_read` per member, not per project** — from the member's actual
invocation, else the prompt lies again for a mixed-vendor team. Read **both**
`repo_read_root` and the legacy `dev_repo_read_root`, since
[Spec 14](SPEC-14-plan.md) Phase 1 renames the key on a parallel branch and the
two must land in either order.

Segment order is fixed by the batch plan's ownership contract — `gate_output`
(A) precedes `tool_guidance` (B) in dev/reviewer/tester.

**Tests.** `test_prompt_segments_golden.py` updated deliberately (the prep PR's
P0.5 made the `_old_*` builders call `tool_catalog_text`, so this is a small
diff); a note in the code that the prompt is composed *before* a mid-turn
retrieval fallback (`async_claude_cli.py:365-380`) can occur, so a fallback turn
may not have the tools its prompt named — harmless, but worth not re-chasing.

## Phase 3 — `tool_not_allowed`: better text **and** a real carry-forward

1. `turn_controller.py:109-121` — the recorded error names the allowed tools and
   the real read path (`"tool_not_allowed: 'read_files' — this role executes
   only: code_write; to read files use Read/Grep/Glob directly"` / `"…use a
   context_request intent"`). This is what an operator reads in `errorta
   decisions` (`runner.py:3293-3300`).
2. **The half that makes it matter.** Persist the last tool failure on the task
   (an explicit field mirroring `reason_summary`) when the turn is requeued to
   `todo` (`runner.py:3293-3316`), and render it as a bounded line in
   `_dev_prompt_segments` (`runner.py:1561-1620`) — next to
   `_latest_context_response_text`, the established "here's what came back from
   your last ask" slot. Clear it on the next successful write so it never becomes
   stale nagging.

This is the F136 case with 352 identical `MemRead: tool_not_allowed` failures on
one task (`runner.py:3308`), and the gravity-golf case where the dev gave up and
wrote `main.js` blind.

**Tests.** An unknown tool records an error naming the real tools; **the next
composed dev prompt for that task contains the failure** (asserted on the prompt,
since no corrective-retry path reaches it); it clears after a successful write;
the turn is still unproductive when no write landed (F136 regression lock).

## Phase 4 — reconcile the `dev_repo_read` default

The **value** was decided in the prep PR (P0.3). Here: make the field
(`autonomy.py:154`) authoritative and correct all three prose sites —
`autonomy.py:144-153`, `autonomy.py:230-233`, `runner.py:2431-2434` — plus a
drift-lock test asserting the field default equals what the docstrings claim and
that `reviewer_repo_read` agrees.

If P0.3 already landed the reconciliation, this phase is just the test.

## Phase 5 — docs

- `docs/coding/PM_REFERENCE.md` — one table of what each role's turn can
  actually do, matching the rendered catalog.
- `python/errorta_cli/SPEC_MAP.md` — add the `Spec 12`–`Spec 18` coordinates so
  the batch's new code comments decode. (B-owned file, batch-wide content —
  include A's four rows.)

## Definition of done

Full coding suite + `ruff` green. Default-args `tool_catalog_text` is
byte-identical to `main`. The carry-forward asserted **on the composed prompt**,
not on the decision record. No `gate_output` segment touched.
