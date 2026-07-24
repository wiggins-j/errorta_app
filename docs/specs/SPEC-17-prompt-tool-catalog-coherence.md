# Spec 17 — Prompt / tool-catalog coherence

**Source:** `docs/coding/RUN_ANALYSIS_GRAVITY_GOLF_2026-07-24.md` §6 **S6 (P2)**
**Target version:** v0.1 (engine)
**Status:** proposed (revised after a code-grounded review — see the **Δ review** notes)
**Owner:** wiggins-j

> **Δ review** found Item 3's stated delivery mechanism does not exist (a
> `tool_not_allowed` failure happens *after* a successful parse, so it never
> reaches the corrective-retry path), and that the `dev_repo_read` default has a
> **fourth** disagreeing statement. Both corrected below. This spec also takes
> ownership of **all** `tool_guidance` prompt segments for the batch, so the two
> parallel branches do not both add the reviewer's.

## Problem

A dev turn burned itself on a contradiction the harness told it.

Three descriptions of the dev's tool surface were live at once:

1. the work request said the dev can read any file in the repo;
2. the tool catalog said *"Available Coding Mode tools for role dev:
   code_write"* (`tool_catalog_text`, `turn_controller.py:69-71`, injected as the
   `tool_guidance` segment at `runner.py:1607-1609`);
3. the **actual** surface, when Spec 11 repo-read is on, is `code_write` **plus**
   native `Read`/`Grep`/`Glob` running with cwd = the task worktree
   (`async_claude_cli.py:64-99`) — invisible in the prompt's framing.

dev-1 resolved the contradiction the way a reasonable model would: it emitted a
tool plan for a hallucinated errorta tool, `read_files`. That is not in
`_ROLE_TOOLS`, so `execute_dev_turn` rejected it `tool_not_allowed`
(`turn_controller.py:109-121`), the turn produced no write, the F136 path
returned an unproductive `noop` (`runner.py:3293-3316`) — and on the retry the
dev wrote `main.js`, the integration-critical wiring file, **blind**. It got the
cross-module contract right anyway, this time.

The same class of bug bit before at scale: the F136 comment records *"352
identical `MemRead: tool_not_allowed` failures on one task"* (`runner.py:3308`).
The rejection is correct and the escalation ladder works; what is missing is
telling the model what the real tools are so it never guesses.

There is a second, smaller incoherence in the same area. The `dev_repo_read`
policy field defaults to `False` (`autonomy.py:154`) while its own docstring two
lines above says *"Default ON"* (`autonomy.py:151`), `policy_from_dict`'s comment
says *"Absent key -> dataclass default (True)"* (`autonomy.py:230-233`), and
`runner.py:2434` says the production runner passes it *"(default True)"*.
**Four** statements, two values, about whether the flagship capability is on.

## Goals

- The work-request framing, the tool catalog, and the real CLI-native tool
  surface **describe the same reality**, derived from one source.
- A model that wants to read the repo is told **how** — with the real tool names
  when repo-read is active, and with the typed `context_request` escape hatch
  when it is not.
- An unknown tool name produces a **corrective hint naming the real tools**,
  not a bare failure the model can only guess its way out of.
- `dev_repo_read`'s default is stated once and is true.

## Non-goals

- Not changing what tools any role has. This spec is descriptive coherence only;
  capability changes are [Spec 12](SPEC-12-in-loop-acceptance-gate.md) and
  [Spec 14](SPEC-14-grounded-reviewer.md).
- Not relaxing `tool_not_allowed`. Fail-closed stays fail-closed — an unknown
  tool is still rejected, it just gets a useful error.
- Not editing the operator's free-text work request
  (`set_work_request`, `ledger.py:1554`). That is the operator's words; this spec
  fixes the harness-authored text around it and makes the truth explicit enough
  that a stale operator claim is corrected rather than compounded.

---

## Item 1 — Capability-aware tool catalog

**Design.** Widen `tool_catalog_text` (`turn_controller.py:69-71`) to take the
live capability picture rather than only `_ROLE_TOOLS`:

```
tool_catalog_text(role, *, repo_read: bool = False, gate: bool = False) -> str
```

It renders, in one block:

- the **executed errorta tools** for the role (unchanged source:
  `allowed_tools_for_role`, `turn_controller.py:35`);
- when `repo_read` is on, the **CLI-native read tools by name** — `Read`, `Grep`,
  `Glob` — stated as available in the current working directory, read-only, and
  *not* to be emitted as errorta tool calls (they are used directly, mid-turn;
  the coding_turn.v1 envelope carries only `code_write`). This distinction is the
  precise thing the current framing omits, and it is what a model must know to
  use the capability correctly;
- when `repo_read` is off, the typed read-only alternative that actually exists:
  the `context_request` dev intent (`schemas.py:167-188`) — a first-class way to
  ask for a file/contract, currently never mentioned in the dev prompt despite
  being fully implemented (`_answer_dev_context_request`, `runner.py:3285-3288`);
- an explicit **negative**: there is no execute/run/shell tool for this role; do
  not plan one. Post-[Spec 12](SPEC-12-in-loop-acceptance-gate.md), the sentence
  names where execution evidence does come from (the gate output segment).

The values come from [Spec 15](SPEC-15-capability-aware-planning.md)'s
`capability_manifest` when that lands, so the PM's view and the worker's view are
the same object. Until then, the two booleans are threaded from the policy.

**Acceptance.** With repo-read on, the dev's `tool_guidance` names Read/Grep/Glob
and says they are used directly, not emitted as tool calls; with it off, it names
`context_request` instead. Every rendering states that no execute tool exists.
`_ROLE_TOOLS` remains the single source for the errorta-tool list.

## Item 2 — Reconcile the `dev_repo_read` default

**Design.** Pick one value, make the dataclass field the single source of truth
(`autonomy.py:154`), and correct **all three** prose statements —
`autonomy.py:144-153`, **`autonomy.py:230-233`** (Δ review: previously missed, and
the impl note scoped the fix to `:144-154`, which would have left it wrong), and
`runner.py:2431-2434`. Add a test asserting the documented default equals the
field default so they cannot drift again.

**Δ review — decide the value in the shared prep PR, not on this branch.**
[Spec 14](SPEC-14-grounded-reviewer.md) Item 1 adds `reviewer_repo_read` and must
match it. If Engineer B picks `True` here while Engineer A has already shipped
`reviewer_repo_read=False`, the flagship capability lands half-on **and both
specs' acceptance criteria still pass**. The batch plan records the decision.

**Acceptance.** The field default and all three prose statements agree; a test
fails if any one changes alone; `reviewer_repo_read` defaults to the same value.

## Item 3 — A corrective hint on `tool_not_allowed`

**Δ review — the original delivery mechanism does not exist.**
`_corrective_turn_prompt` (`runner.py:541`) is reached only from
`_parse_member_turn`'s retry loop (`runner.py:2540-2556`), gated on a
`TurnParseError` whose code is in `_RETRYABLE_TURN_ERRORS` (`runner.py:515-519`
= `turn_non_json`, `turn_tool_markup_only`, `turn_schema_mismatch`). A
`tool_not_allowed` failure happens **after** a successful parse, inside
`execute_dev_turn` (`turn_controller.py:110-123`); the runner records a decision,
returns the task to `todo`, and the re-dispatch builds a **fresh** `_dev_prompt`
(`runner.py:1552`) with no memory of the failure. (`runner.py:511` is a comment
for `_WORKER_CORRECTIVE_RETRIES`, not a code path.) So the error text alone
changes nothing the model ever sees.

**Design.** Two halves:

1. **Better error text.** When `execute_dev_turn` rejects an unknown tool
   (`turn_controller.py:109-121`), include the allowed tool names in the recorded
   error (`"tool_not_allowed: 'read_files' — this role executes only:
   code_write; to read files use Read/Grep/Glob directly"` / `"…use a
   context_request intent"`). This flows into the decision record
   (`runner.py:3293-3300`) and is what an operator reads in `errorta decisions`.
2. **A real carry-forward, so the *model* sees it.** Persist the last tool
   failure on the task (an explicit `add_task`/`update_task` field, mirroring
   `reason_summary`) when the turn is requeued to `todo`
   (`runner.py:3293-3316`), and render it as a bounded line in
   `_dev_prompt_segments` (`runner.py:1561-1620`) — next to the existing
   `_latest_context_response_text` slot, which is the established
   "here is what came back from your last ask" channel. Cleared on the next
   successful write so it never becomes stale nagging.

**Acceptance.** A rejected unknown tool records an error naming the real tools;
**the next dispatch of that task carries the failure in its prompt** (asserted on
the composed prompt, not on the decision record); the failure clears after a
successful write; the rejection itself is unchanged (still fail-closed, still
unproductive when nothing was written).

---

## Implementation notes

- **`turn_controller.py`** — widen `tool_catalog_text` (`:69-71`); enrich the
  `tool_not_allowed` error (`:109-121`). `_ROLE_TOOLS` (`:27-32`) and its
  F087-14 WS-3 rationale (`:20-26`) are untouched — this spec extends that
  discipline rather than amending it.
- **`runner.py`** — pass the live flags at the `tool_guidance` segment in
  `_dev_prompt_segments` (`:1607-1609`); add the equivalent segment to
  `_review_pr_prompt_segments` (`:1713-1775`) and `_test_prompt_segments`
  (`:2077-2116`), which have none today (**this spec owns every `tool_guidance`
  segment in the batch**; `gate_output` segments belong to
  [Spec 12](SPEC-12-in-loop-acceptance-gate.md) — see the batch plan's segment
  order contract); the tool-failure carry-forward (`:3293-3316`, rendered in
  `:1561-1620`); correct the `dev_repo_read` docstring (`:2431-2434`).
- **`autonomy.py`** — reconcile the field default and **both** prose statements
  (`:144-154` and `:230-233`).
- **Golden tests** — `test_prompt_segments_golden.py` byte-locks all four
  prompts (confirmed); the updates are deliberate and the goldens change with
  them. Keep the *off* rendering as close to today's string as the content allows
  so the diff is legible. Cheap de-conflicting measure: make the `_old_*`
  reference builders **call** `tool_catalog_text` instead of inlining its string,
  so a later gate-segment change touches a different line.

## Edge cases

- **A vendor without repo-read** (codex/cursor): `repo_read=False` for that
  member's turn, so the catalog offers `context_request` — correct per member,
  not per project. The flag must be resolved from the **member's actual
  invocation**, not only the policy, or the prompt lies again for a mixed-vendor
  team. That resolution reads the member's repo-read root key, which
  [Spec 14](SPEC-14-grounded-reviewer.md) Item 1 renames
  `dev_repo_read_root` → `repo_read_root` on a parallel branch — **read both
  keys** so the two land in either order.
- **Retrieval falls back mid-turn** (the turn budget is exhausted and the
  provider re-runs plain, `async_claude_cli.py:365-380`): the prompt was composed
  before the fallback, so it may name tools that turn did not get. Harmless — the
  fallback turn produces a normal envelope — but worth a sentence in the code so
  the next reader does not chase it.
- **The operator's work request contradicts the catalog** (the observed case):
  the catalog is now specific enough to be the operative statement, and
  [Spec 15](SPEC-15-capability-aware-planning.md)'s capability block in the PM
  prompt reduces how often a stale claim is written in the first place. Rewriting
  operator text stays out of scope.

## Testing

- **Item 1**: rendered catalog for dev/reviewer/tester × repo-read on/off ×
  gate on/off (table-driven); every variant contains the no-execute-tool
  sentence; the errorta-tool list is exactly `allowed_tools_for_role`; prompt
  goldens updated.
- **Item 2**: field default == documented default (the drift lock);
  `policy_from_dict({})` yields it.
- **Item 3**: an unknown tool records an error naming the allowed tools; **the
  next composed dev prompt for that task contains the failure** (asserted on the
  prompt, since no corrective-retry path reaches it); the carry-forward clears
  after a successful write; the turn is still unproductive when no write landed
  (regression lock on the F136 behavior).
- **The repro**: a dev turn emitting `read_files` gets, on retry, a prompt that
  names the real read path — asserted against today's behavior (a bare
  `tool_not_allowed` and a blind rewrite).
- Full coding suite + `ruff`.

## Documentation

- `docs/coding/PM_REFERENCE.md`: one table of what each role's turn can actually
  do, matching the rendered catalog.
- `python/errorta_cli/SPEC_MAP.md`: add the `Spec 12`–`Spec 18` coordinates when
  the batch lands, so the new comment references decode.

## Out of scope / follow-ups

- Surfacing the CLI-native tool surface in the UI's context inspector.
- Making `context_request` available to the reviewer and tester (it is a dev
  intent today, `schemas.py:167`); after
  [Spec 14](SPEC-14-grounded-reviewer.md) the reviewer reads directly and does
  not need it, but the tester might.
- Auto-rewriting a stale operator work request that contradicts the live
  capability set.
