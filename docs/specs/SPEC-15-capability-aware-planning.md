# Spec 15 — Capability-aware planning and finding routing

**Source:** `docs/coding/RUN_ANALYSIS_GRAVITY_GOLF_2026-07-24.md` §6 **S4 (P1)**
**Target version:** v0.1 (engine)
**Status:** proposed (revised after a code-grounded review — see the **Δ review** notes)
**Owner:** wiggins-j

> With a gate present, execution-imperative work is *routed*; without one, it is
> *rejected at planning time*. Both are improvements on assigning it to a dev.
>
> **Δ review:** this spec has **no hard dependency on
> [Spec 12](SPEC-12-in-loop-acceptance-gate.md)** — "is there something to run"
> is already answerable on main via `evidence._tests_required`
> (`evidence.py:115-127`), and Item 3's gate-*triggering* half (the only real
> cross-branch blocker) is descoped below. This spec can be built and merged from
> a separate branch without waiting for Spec 12.

---

## Problem

The PM planned a task titled **"Run acceptance gate and fix failures"** and
assigned it to a DEV. A DEV has exactly one tool — `code_write`
(`_ROLE_TOOLS`, `turn_controller.py:27-32`). The task is unsatisfiable as
written, and the loop had no way to notice: it dispatched, the dev wrote code
(all it can do), the reviewer rejected for missing execution evidence, a
`revise:` task was spawned, and the cycle repeated on ~2-minute intervals for the
last ~20 minutes of the run.

Nothing in the pipeline models **what each role can do**:

- The PM prompt (`_pm_prompt_segments`, `runner.py:1290-1340`) tells the PM what
  *not* to create — *"do NOT create reviewer/tester/merge tasks"* — and to write
  acceptance criteria and in-scope files. It never states what a DEV is
  physically capable of, so "run the gate" reads as a perfectly ordinary task.
- `_materialize_pm_tasks` (`runner.py:1366-1400`) gates on duplicates (Spec 08)
  and injects path dependencies (Spec 09), but has no capability lint.
- Reviewer findings become `revise:` task text verbatim
  (`_reason_from_findings` / `_detail_from_findings`, `runner.py:472-500`,
  consumed at `runner.py:3470-3483`), so *"no strokes-per-level table"* becomes a
  DEV work item with no filter in between.

The skill directives make it worse in a quietly ironic way. The TESTER's
directive is *"Run the code and confirm it actually does what it should before
marking anything complete"* (`skills.py:44-46`) and the DEV's is *"A task is not
done until its test passes"* (`skills.py:29-31`) — both injected into every turn,
both currently unsatisfiable from inside a turn.

## Goals

- One **single source of truth** for role capabilities, derived from the code
  that actually enforces them, rendered into the prompts that need it.
- An **execution imperative** in a task or a finding is routed to whatever can
  execute (post-[Spec 12](SPEC-12-in-loop-acceptance-gate.md): the in-loop gate /
  the tester) — or refused at planning time with a legible reason — never
  silently assigned to a write-only dev.
- The PM learns about the refusal and can re-plan, instead of the loop absorbing
  it as a spiral.

## Non-goals

- Not a new permission system. Capabilities are *described* from `_ROLE_TOOLS`
  plus the live policy flags; enforcement stays exactly where it is
  (`turn_controller.execute_dev_turn`, the CLI tool allowlist).
- Not natural-language task validation in general. The lint targets one narrow,
  high-signal class: *"produce evidence by executing something"*.
- Not blocking the PM from planning test-related work. "Write an acceptance test
  for level triviality" is a perfect DEV task and must stay one. The distinction
  is **write a test** (dev) vs **run it and report the output** (gate/tester).

---

## Item 1 — A role-capability manifest, derived not authored

**Design.** New `coding/capabilities.py`, pure and read-only:

```
capability_manifest(store, policy) -> dict[str, RoleCapability]
```

Each `RoleCapability` carries: the executed tool names
(`allowed_tools_for_role`, `turn_controller.py:35`), whether in-turn repo read
is active for that role (`policy.dev_repo_read` / `policy.reviewer_repo_read`),
whether a gate exists, and a plain one-line "what this role can and cannot do".

**Δ review — read both cross-branch inputs defensively so this ships first.**
`reviewer_repo_read` is added by the shared prep PR but consumed by
[Spec 14](SPEC-14-grounded-reviewer.md); read it as `getattr(policy,
"reviewer_repo_read", False)`. Gate availability comes from
`gate_state.gate_available(store)` — the shared read-only seam the prep PR lands,
whose v1 body is today's `evidence._tests_required` (`evidence.py:115-127`) and
which [Spec 12](SPEC-12-in-loop-acceptance-gate.md) later enriches without
changing the signature.

Derivation, not authorship, is the point: when `_ROLE_TOOLS` changes, every
prompt that describes it changes with it, and the F087-14 WS-3 discipline
("advertise ONLY tools that are actually executed") extends from the tool catalog
to the planner.

Rendered into:
- the PM prompt as a `tool_guidance` segment (`runner.py:1290-1340`), replacing
  nothing — additive, listing each role's real surface and stating explicitly
  that **no role can run a command from inside a turn**; where execution
  evidence is needed, the PM must rely on the gate;
- the dev/reviewer/tester `tool_guidance` segments via
  [Spec 17](SPEC-17-prompt-tool-catalog-coherence.md), which consumes the same
  manifest so the two can never drift.

**Acceptance.** `capability_manifest` reflects `_ROLE_TOOLS` and the live policy
flags; flipping a policy flag changes the rendered text; adding a tool to
`_ROLE_TOOLS` changes it with no edit to any prompt string.

## Item 2 — Execution-imperative lint at task materialization

**Design.** `capabilities.classify_task_text(title, detail) -> "execution" |
"authoring" | "other"`, applied at the task-creation chokepoints. **Δ review:
there are three, not two** — `_materialize_pm_tasks` (`runner.py:1366-1487`,
beside the Spec 08 dedupe gate); `control_actions.create_task`
(`control_actions.py:341`, reached only from the PM chat control-action surface);
and `POST /coding/projects/{id}/tasks` → `add_task` (`routes/coding.py:1408-1419`),
which is what `errorta task new` (`errorta_cli/commands/task.py:36`) hits and
which bypasses even Spec 08's dedupe today. v1 lints all three; the HTTP one
returns the refusal to the caller rather than swallowing it (see Edge cases).

`"execution"` requires an imperative verb about *running* (`run`, `execute`,
`launch`, `measure`, `benchmark`, `profile`, `verify by running`) taking the
artifact as object, **and** an evidence demand (`paste`, `report`, `output`,
`table`, `results`, `confirm it passes`). Both halves are required so *"write a
script that runs the levels"* (authoring) does not match. `"authoring"` —
`write`/`add`/`create` a test/gate/harness — is explicitly whitelisted; it is
normal, valuable DEV work.

Then, for a DEV task classified `"execution"`:

- **Gate available** (post-Spec 12): keep the fix half, drop the run half.
  Rewrite the task to *"Fix the failures reported by the acceptance gate"*, and
  bind it to the gate output ([Spec 12](SPEC-12-in-loop-acceptance-gate.md)
  Item 3 puts the verbatim result in the prompt once it lands; until then
  `gate_state.latest_gate_text` renders whatever runs exist). Record a decision
  (`choice="task_routed_to_gate"`).
- **No gate**: refuse the task at creation, record
  `choice="task_requires_absent_capability"` with the offending text and the
  reason, and surface it to the PM through the existing dedupe-report channel
  (`runner.py:1191`, "the honest report of what the gate threw away") so the next
  plan turn sees *why* — the same shape Spec 08 already established for
  duplicates.

**Acceptance.** *"Run acceptance gate and fix failures"* assigned to a DEV is
rewritten (gate present) or refused with a legible reason (no gate). *"Write an
acceptance test that fails on trivial levels"* is untouched. *"Run the linter"*
with no evidence demand is untouched (no evidence half). A TESTER task is never
linted — running is its job.

## Item 3 — The same lint on reviewer findings

**Design.** At the reviewer-rejection branch (`runner.py:3459-3484`), classify
each blocking finding's title+body before it becomes `revise:` task text.

A finding classified `"execution"` — *"no evidence that the tests were actually
run; no strokes-per-level table"* — does **not** spawn a DEV `revise:` task. The
same suppression applies to a rejection whose blocking findings are all
`cited: false` ([Spec 14](SPEC-14-grounded-reviewer.md) Item 3 produces that
flag; **this spec owns the suppression**, so the shared seam has exactly one
writer on the consumer side). The PR still goes `changes_requested` — nothing
auto-merges; only the DEV rework task is withheld.

Instead:

- **with a gate:** attach the most recent existing gate result
  (`gate_state.latest_gate_run(store)`) and re-queue the review with that output
  in prompt, so the reviewer's demand is *satisfied* rather than forwarded.
  **Δ review — two corrections.** (a) The original said "trigger a gate run for
  the PR's head", which needs Spec 12's `_run_inloop_gate` — the batch's only
  hard cross-branch code dependency. Reading the newest recorded run is a plain
  `list_test_runs()` read that works on main today, and once Spec 12 lands the
  record is simply fresher. (b) A re-queued review whose verdict can be *another*
  execution-demand finding is an unbounded loop that
  [Spec 16](SPEC-16-revise-chain-circuit-breaker.md)'s breaker cannot see (it
  counts revise lineages, not re-review rounds). So: **at most one re-review per
  PR head**; a second execution-demand finding escalates to the PM. This batch
  exists because of a livelock — it must not add one.
- **without a gate:** record `choice="finding_requires_absent_capability"` and
  escalate to a PM re-plan turn with the finding attached.

Either way the finding is preserved verbatim in the decision record — it is not
suppressed, it is *routed*.

**Acceptance.** An execution-demand blocking finding creates no DEV `revise:`
task and produces exactly one re-review for that head (gate present) or one PM
escalation (no gate); a second such finding on the same head escalates rather
than re-reviewing again — the re-review-loop lock. An all-uncited rejection is
suppressed identically. The PR is `changes_requested` throughout and never
mergeable. An ordinary code finding behaves exactly as today.

---

## Implementation notes

- **New:** `coding/capabilities.py` — pure, imports `.topology`,
  `.turn_controller` (for `allowed_tools_for_role`), `.gate_state`, and takes
  `store`/`policy` as arguments. Watch the import direction: `runner` imports
  `.topology`/`.schemas` (`runner.py:43-45`), so `capabilities` must not import
  `runner` — same discipline as `coding/paths.py` (F159).
- **`runner.py`** — capability segment in `_pm_prompt_segments` (`:1290-1344`);
  lint in `_materialize_pm_tasks` (`:1366-1487`, next to the Spec 08 gate);
  finding classification + revise suppression in the shared rejection seam
  (`:3455-3485`); refusal reporting through the existing planner-feedback path
  (`_duplicate_rejection_note`, `:1190-1222`).
- **`control_actions.py`** — same lint at `create_task` (`:341`).
- **`routes/coding.py`** — same lint at `add_task` (`:1408-1419`), returning the
  refusal to the caller.
- **No new policy knob** in v1 — the lint is always on, because a task no role
  can perform is never correct. If a kill switch proves necessary, add
  `capability_lint: bool` at that point rather than pre-emptively.
- **`skills.py`** — no code change, but the TESTER/DEV directives (`:29-46`) are
  only honest once [Spec 12](SPEC-12-in-loop-acceptance-gate.md) lands; note the
  dependency in that spec's docs rather than softening the directives.

## Edge cases

- **False positive on legitimate work.** The two-half requirement (run-verb +
  evidence-demand) is deliberately narrow, and the failure mode is bounded: with
  a gate the task is *rewritten*, not dropped; without one it is refused with the
  text preserved in a decision the PM sees. No work is silently lost.
- **A PM that re-plans the same refused task.** Spec 08's dedupe catches the
  restatement; the refusal decision gives the PM the reason. If it still loops,
  Spec 07's `planning_churn` detector (`_account_planning_churn`,
  `autonomy.py:878`) is the backstop.
- **An operator-authored task** via `errorta task new`
  (`errorta_cli/commands/task.py:36` → `POST .../tasks` →
  `routes/coding.py:1408-1419` → `LedgerStore.add_task` directly — Δ review: it
  does **not** pass through `control_actions.create_task`): linted at the route,
  and a refusal is returned to the caller (a 422 with the reason), never
  swallowed. The operator can then rephrase or use the gate.
- **Governance-sourced tasks** with a `done_when` that demands execution: same
  routing; the slice's acceptance bar is satisfied by gate output rather than by
  a dev's assertion, which is strictly better grounding.
- **A finding that is half execution-demand, half real defect** (*"the canvas is
  0×0 — run it and see"*): classified `"execution"` only if the run-verb takes the
  artifact as object *and* the evidence demand is the ask. A finding naming a
  concrete defect with a path stays a normal finding (and
  [Spec 14](SPEC-14-grounded-reviewer.md) Item 3 requires that path anyway).

## Testing

- **Item 1**: manifest reflects `_ROLE_TOOLS` and policy flags; adding a tool
  changes the rendered PM text with no prompt-string edit; PM prompt golden
  updated deliberately.
- **Item 2 (classifier table)**: `"Run acceptance gate and fix failures"` →
  execution; `"Write an acceptance test for level triviality"` → authoring;
  `"Run the linter"` → other (no evidence half); `"Measure and report frame
  time"` → execution; `"Add a benchmark harness"` → authoring. Table-driven, and
  the table is the spec of the lint.
- **Item 2 (integration)**: with a gate, the task is rewritten and its prompt
  carries gate output; without, it is refused with a decision and reported to the
  PM; a TESTER task with identical text is untouched.
- **Item 3**: an execution-demand blocking finding produces no `revise:` DEV task
  and does produce one re-review (gate) or one PM escalation (no gate); a second
  execution-demand finding on the same head escalates instead of re-reviewing
  (the re-review-loop lock); an all-uncited rejection is suppressed the same way;
  the PR is `changes_requested` and never mergeable throughout; an ordinary
  finding produces today's `revise:` task (regression lock).
- **The repro**: replay the observed `revise: task-t-6ad32880fe5d` chain and
  assert it terminates — no fourth-generation revise.
- Full coding suite + `ruff`.

## Documentation

- `docs/coding/PM_REFERENCE.md`: the role-capability table (what each role can
  actually do), and the rule that execution evidence comes from the gate — with
  the two new decision choices (`task_routed_to_gate`,
  `task_requires_absent_capability`).
- `docs/CLI.md`: refused tasks appear in `errorta decisions` with their reason.

## Out of scope / follow-ups

- A general task-quality linter (vagueness, missing acceptance criteria).
- Capability negotiation — a role *requesting* a capability it lacks. The dev
  already has a typed read-only escape hatch for context
  (`DeveloperContextRequestIntent`, `schemas.py:167-188`); an execution analogue
  is a real design question, not a v1 item.
- Teaching the PM to plan gate-shaped work proactively (e.g. always ending a
  milestone with a gate-verified slice).
