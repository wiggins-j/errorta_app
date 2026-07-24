# Spec 15 — Implementation plan (capability-aware planning + finding routing)

Spec: [SPEC-15-capability-aware-planning.md](SPEC-15-capability-aware-planning.md).

**Owner:** Engineer B · **Branch:** `feat/spec-15-capability-aware-planning`
**Base:** `chore/spec-12-18-prep` (merged) · **PR into:** `main`
**Land after** [Spec 17](SPEC-17-plan.md) (shared prompt-segment surface) **and
after Engineer A's Spec 14 merges**, because Phase 3 reads the `cited` flag Spec
14 produces. Phases 1–2 have no such dependency and can be built first.

The spec's **Δ review** notes that shape this plan:

- **No hard dependency on Spec 12.** "Is there a gate?" is
  `gate_state.gate_available` from the prep PR (v1 body = today's
  `evidence._tests_required`). Item 3's gate-*triggering* half — the batch's only
  hard cross-branch code dependency — is descoped to reading the latest recorded
  run.
- **Three task-creation chokepoints, not two.** `errorta task new` goes straight
  to `LedgerStore.add_task` via the route and bypasses even Spec 08's dedupe.
- A re-queued review can itself return another execution-demand finding — an
  unbounded loop Spec 16's breaker cannot see. Cap it.

## Phase 0 — spec + plan (no code)

Branch off the merged prep PR; commit the spec + this plan.

## Phase 1 — `coding/capabilities.py` (pure, no wiring)

```
capability_manifest(store, policy) -> dict[str, RoleCapability]
```

Each `RoleCapability` derives from code, never prose: executed tool names from
`allowed_tools_for_role` (`turn_controller.py:35`); repo-read per role from
`policy.dev_repo_read` / `getattr(policy, "reviewer_repo_read", False)`
(defensive — the field exists after the prep PR, its consumer ships on A's
branch); gate availability from `gate_state.gate_available(store)`; a one-line
"can / cannot".

Imports `.topology`, `.turn_controller`, `.gate_state` — **never `runner`**
(`runner` imports `.topology`/`.schemas` at `runner.py:43-45`; F159's
`coding/paths.py` set the precedent).

**Tests** (`test_spec15_capabilities.py`, new): the manifest reflects
`_ROLE_TOOLS` and the live policy flags; **adding a tool to `_ROLE_TOOLS` changes
the rendered text with no prompt-string edit** (the derivation lock); a policy
missing `reviewer_repo_read` does not raise.

## Phase 2 — the execution-imperative classifier + lint

1. `capabilities.classify_task_text(title, detail) -> "execution" | "authoring" |
   "other"`. `"execution"` requires **both** a run-verb taking the artifact as
   object (`run`, `execute`, `launch`, `measure`, `benchmark`, `profile`, `verify
   by running`) **and** an evidence demand (`paste`, `report`, `output`, `table`,
   `results`, `confirm it passes`). `"authoring"` (`write`/`add`/`create` a
   test/gate/harness) is explicitly whitelisted — normal, valuable DEV work.
2. Apply at all three chokepoints:
   - `_materialize_pm_tasks` (`runner.py:1366-1487`), beside the Spec 08 gate;
   - `control_actions.create_task` (`control_actions.py:341`);
   - `add_task` route (`routes/coding.py:1408-1419`) — returns the refusal to the
     caller (422 + reason), never swallows it, since `errorta task new`
     (`errorta_cli/commands/task.py:36`) hits this path.
3. For a DEV task classified `"execution"`:
   - gate available → rewrite to the fix half ("Fix the failures reported by the
     acceptance gate"), record `task_routed_to_gate`;
   - no gate → refuse, record `task_requires_absent_capability`, report to the PM
     through the existing planner-feedback channel (`_duplicate_rejection_note`,
     `runner.py:1190-1222`).
   TESTER tasks are never linted — running is their job.

**Tests (table-driven — the table *is* the spec of the lint):** "Run acceptance
gate and fix failures" → execution; "Write an acceptance test for level
triviality" → authoring; "Run the linter" → other (no evidence half); "Measure
and report frame time" → execution; "Add a benchmark harness" → authoring.
Integration: with a gate the task is rewritten and its prompt carries gate text;
without, refused + reported; an identically-worded TESTER task is untouched; the
HTTP route returns 422 with a reason.

## Phase 3 — finding routing *(needs Spec 14 merged)*

In the shared `_handle_review_rejection` seam (prep PR; originally
`runner.py:3455-3485`) — **outputs side only; Engineer A owns the inputs side**:

1. Classify each blocking finding's title+body with the Phase-2 classifier.
2. Suppress the DEV `revise:` task when **either** every blocking finding is
   `"execution"`-class **or** every blocking finding is `cited: false` (Spec 14
   Item 3's flag — this branch owns the suppression, Spec 14 owns only the flag).
   The PR still goes `changes_requested`; nothing auto-merges.
3. Route instead:
   - gate available → attach `gate_state.latest_gate_run(store)` and re-queue the
     review with that output in prompt. **At most one re-review per PR head**; a
     second execution-demand finding on the same head escalates to the PM.
   - no gate → record `finding_requires_absent_capability` + one PM escalation.
4. The finding text is preserved verbatim in the decision record — routed, never
   suppressed as information.

**Tests.** Execution-demand blocking finding → no `revise:` task, exactly one
re-review (gate) or one PM escalation (no gate); a second such finding on the
same head escalates instead of re-reviewing (**the re-review-loop lock**); an
all-uncited rejection is suppressed identically; the PR is `changes_requested`
and never mergeable throughout; an ordinary code finding produces today's
`revise:` task (regression lock).

## Phase 4 — PM capability block

The manifest rendered as a `tool_guidance` segment in `_pm_prompt_segments`
(`runner.py:1290-1344`) — additive, listing each role's real surface and stating
explicitly that **no role can run a command from inside a turn**, and where
execution evidence comes from instead.

This is a `tool_guidance` segment, which Engineer B owns batch-wide (see the
batch plan's ownership contract) — Spec 17 supplies the renderer, this phase
supplies the PM placement.

**Tests.** PM prompt golden updated deliberately; the rendered block changes when
a policy flag flips.

## Phase 5 — the repro + docs

- Replay the observed `revise: task-t-6ad32880fe5d` chain and assert it
  terminates — no fourth-generation revise.
- `docs/coding/PM_REFERENCE.md` — the role-capability table; execution evidence
  comes from the gate; the two new decision choices.
- `docs/CLI.md` — refused tasks appear in `errorta decisions` with their reason;
  `errorta task new` can now be refused with a reason.

## Definition of done

Full coding suite + `ruff` green. The re-review-loop lock asserted. No
`gate_output` segment touched (Engineer A owns those). No edit to the inputs side
of the rejection seam.
