# Spec 12–18 — gravity-golf batch: sequencing, ownership, and parallel-build contract

**Source:** `docs/coding/RUN_ANALYSIS_GRAVITY_GOLF_2026-07-24.md` §6
**Status:** proposed (revised after a code-grounded review)
**Owner:** wiggins-j

The run analysis's fix list (`S1`–`S7`) written up as seven specs, continuing the
`Spec NN` series the code comments cite (Spec 01–11 — see
`python/errorta_cli/SPEC_MAP.md` for the coordinate convention).

| Spec | Plan | Analysis | Prio | Engineer | Branch | What it fixes |
|---|---|---|---|---|---|---|
| — | [prep](SPEC-12-18-prep-plan.md) | — | — | **A** | `chore/spec-12-18-prep` | De-conflicts the two branches; no behavior change |
| [12](SPEC-12-in-loop-acceptance-gate.md) | [plan](SPEC-12-plan.md) | S1 | P0 | **A** | `feat/spec-12-in-loop-gate` | The gate never runs inside the loop, so "iterate until green" has no feedback signal |
| [13](SPEC-13-foundation-gate-buildless-web.md) | [plan](SPEC-13-plan.md) | S2 | P0 | **A** | `feat/spec-13-buildless-foundation` | A buildless web project can never be foundation-ready → concurrency clamped to 1 all run |
| [14](SPEC-14-grounded-reviewer.md) | [plan](SPEC-14-plan.md) | S3 | P0 | **A** | `feat/spec-14-grounded-reviewer` | The reviewer cannot see the repo → 6-second empty approvals and impossible rejections |
| [15](SPEC-15-capability-aware-planning.md) | [plan](SPEC-15-plan.md) | S4 | P1 | **B** | `feat/spec-15-capability-aware-planning` | Tasks/findings demanding execution get assigned to a write-only dev |
| [16](SPEC-16-revise-chain-circuit-breaker.md) | [plan](SPEC-16-plan.md) | S5 | P1 | **B** | `feat/spec-16-revise-breaker` | Revise-of-revise chains livelock invisibly to every wedge detector |
| [17](SPEC-17-prompt-tool-catalog-coherence.md) | [plan](SPEC-17-plan.md) | S6 | P2 | **B** | `feat/spec-17-tool-catalog-coherence` | Three descriptions of the dev's tools disagree → a hallucinated tool, a blind write |
| [18](SPEC-18-cli-status-unbound-directory.md) | [plan](SPEC-18-plan.md) | S7 | P2 | **B** | `feat/spec-18-status-unbound` | `errorta status` from an unbound dir reports nothing while a run is live |

Every spec and plan is one PR into `main`, reviewed before approval. Both
engineers branch off the merged prep PR.

Product-side fixes for the artifact this run produced are in
[`../coding/GRAVITY_GOLF_PRODUCT_FIXES.md`](../coding/GRAVITY_GOLF_PRODUCT_FIXES.md)
— four small changes that double as the acceptance fixture for Spec 12.

---

## The one-sentence diagnosis

The run was healthy by every counter the harness tracks — 0 `member_failed`, 27
tasks with no duplicate spiral, conflict-free merges, an aligned cross-module
contract — and still produced an un-run artifact with a black-screen init race,
because **nobody in the loop can execute anything**. Roles multiply coordination;
only execution multiplies quality.

The correction the code review produced: the harness **does** execute, correctly,
and even deterministically over the whole registry against the merged tree — in
`delivery_review` (`runner.py:3984-4010`), **once, at the very end**, on a
registry that only the app UI ever fills. Runtime detection has the same shape:
`runtime.detect` (`runtime.py:1293`) would propose the right profile and is
called only from an HTTP route. **This is a wiring gap, not a missing
capability** — which is why Spec 12 is bootstrap-and-schedule, not a new
execution subsystem, and why Spec 14 generalizes Spec 11's existing read-only CLI
mechanism rather than building one.

---

## Phase 0 — the shared prep PR (do this first, one owner, no behavior change)

**Δ review recommendation, adopted.** Four small refactors land on `main` before
either feature branch starts. Each is behavior-preserving and independently
reviewable, and together they remove every certain merge conflict between the two
branches. Engineer A owns this PR; Engineer B branches off it.

**P0.1 — Extract the review-rejection seam.** `runner.py:3455-3485` (and its
strict-mode PM twin at `:3641-3653`) is edited by **four specs across both
engineers** — the batch's single worst conflict, ~30 lines. Pull it into one
function, body verbatim:

```
_handle_review_rejection(store, workspace, *, pr, task, findings, source) -> None
```

with a test asserting identical behavior. A then edits its **inputs** (finding
`cited` flags, foundation-scope classification); B edits its **outputs** (whether
a revise is spawned and what replaces it). Two disjoint edits instead of one
four-way collision.

**P0.2 — Land all seven policy fields with no consumers.** Every spec adds knobs
to the same ten lines of `CodingAutonomyPolicy` (`autonomy.py:63`) plus
`policy_to_dict` / `policy_from_dict` (`:157`, `:183`) — a guaranteed textual
conflict. Add them all at once, defaults only:

| field | default | spec | clamp |
|---|---|---|---|
| `gate_bootstrap` | `True` | 12 | bool |
| `gate_min_merge_interval` | `3` | 12 | `max(1, …)` |
| `reviewer_repo_read` | *see P0.3* | 14 | bool |
| `review_min_latency_ms` | `0` (off) | 14 | `max(0, …)` |
| `review_screenshot` | `False` | 14 | bool |
| `revise_chain_limit` | `3` | 16 | `max(0, …)` = disable |
| `revise_livelock_limit` | `5` | 16 | `max(0, …)` = disable |

**P0.3 — Decide `dev_repo_read`'s default here, in writing.** It disagrees with
itself in four places (`autonomy.py:154` field `False`; `:151` docstring "Default
ON"; `:230-233` comment "dataclass default (True)"; `runner.py:2434` "(default
True)"). Spec 17 Item 2 owns the reconciliation, but the **value** must be fixed
before A ships `reviewer_repo_read`, or the capability lands half-on with both
specs' acceptance criteria passing. Decide, reconcile all four statements, and
set `reviewer_repo_read` to match.

**P0.4 — Land the shared read-only gate seam.** A new `coding/gate_state.py`
(imports no `runner` — F159 `coding/paths.py` discipline):

```
def gate_available(store) -> bool            # v1 body == evidence._tests_required
def latest_gate_run(store) -> dict | None    # newest list_test_runs() record
def latest_gate_text(store, *, cap: int) -> str
```

A enriches `latest_gate_text`'s content in Spec 12; **B consumes all three from
day one with no dependency on A.** This is what removes the batch's last
cross-branch code dependency.

**P0.5 (optional, cheap).** Split `test_prompt_segments_golden.py` per prompt, or
make its `_old_*` reference builders *call* `tool_catalog_text` /
`gate_state.latest_gate_text` rather than inlining their strings. Both branches
change prompt segments this file byte-locks; either measure keeps the two edits
on different lines.

---

## Ownership contract (read this before writing code)

**Prompt segments.** One owner per segment kind, across the whole batch:

| segment | owner | prompts |
|---|---|---|
| `gate_output` | **A** (Spec 12 Item 3) | dev, reviewer, tester |
| `tool_guidance` | **B** (Spec 17 Item 1) | dev, reviewer, tester, PM capability block |

Spec 14 Item 2 explicitly **gave up** the reviewer `tool_guidance` sentence it
originally proposed; Spec 17 renders it from the capability catalog with
`repo_read=policy.reviewer_repo_read`, so it is correct whichever way P0.3 goes.
Segment order both sides code against:

```
dev:      work_request, project_context×2, prior_outputs, repo_snapshot, gate_output(A), tool_guidance(B), role_instructions
reviewer: role_instructions, work_request, project_context, review_rules, pr_diff, gate_output(A), trunc_note, tool_guidance(B), envelope
tester:   work_request, project_context×2, gate_output(A), tool_guidance(B), role_instructions
```

**The review-rejection seam.** A owns what goes *in* (Spec 13's foundation-scope
classification, Spec 14's `cited` flags). B owns what comes *out* (Spec 15's
revise suppression + routing, Spec 16's breaker). **Spec 14 Item 3 does not
suppress the revise task** — it only produces the flag Spec 15 reads. One writer
per side.

**The PR record.** A lands Spec 13's `unlocks_foundation` and Spec 14's
review-grounding fields in **one commit** (`record_pr`, `ledger.py:1017`) — they
add keys to the same dict literal and would otherwise conflict inside A's own
branch. B's Spec 16 uses explicit `add_task` kwargs (`ledger.py:686-730`), not
`_extras` mutation, so it touches a different function.

**`errorta_cli/render/status.py`.** Both of B's Specs 16 and 18 touch it, but in
**different lines and either order** (Δ2 — an earlier revision of this plan said
"land 16 before 18", which contradicted the recommended sequence and created a
circular constraint):

- **Spec 18 owns `_TERMINAL_BAD`** (`:26-30`) — including backfilling
  `gate_not_improving` / `planning_churn` / `dispatch_wedged`, which Specs
  04/07/10 added without updating the set. Its only consumer is the stop-reason
  styling at `:68`, which is Spec 18's own surface.
- **Spec 16 appends one line** to that set for `revise_livelock`.
- Spec 18 also owns the unbound early return (`:54-57`).

**`tool_catalog_text` signature.** Spec 17 makes `repo_read` / `gate` **required**
keyword arguments and the rendering deliberately changes (Δ2 — an earlier
revision asked for defaults *and* a byte-identical default rendering *and* a
mandatory sentence in every variant, which is unsatisfiable: Python cannot
distinguish an omitted default from an explicit `False`). The invariant that
replaces the byte-identical lock: the errorta-tool list in any rendering equals
`", ".join(allowed_tools_for_role(role)) or "none"`.

---

## Dependency graph after the prep PR

```
Phase 0 prep PR (A) ──> main
                         │
  Engineer A            │            Engineer B
  ──────────            │            ──────────
  Spec 13  (independent)│            Spec 17  (independent)
  Spec 12  (independent)│            Spec 18  (independent)
  Spec 14  (14.5/14.6 ← 12.3)        Spec 15  (reads gate_state — no A dep)
                                     Spec 16  (independent)
```

**Cross-group dependencies, all resolved to soft:**

| Dependency | Resolution |
|---|---|
| Spec 15 Item 1 reads `policy.reviewer_repo_read` (A) | `getattr(policy, …, False)`; field exists after P0.2 |
| Spec 15/17 "is there a gate?" | `gate_state.gate_available` (P0.4) — on main, no A dep |
| Spec 15 Item 3 wanted to *trigger* a gate run (A's code) | **descoped** — reads `latest_gate_run` instead; triggering becomes a Spec 12 follow-up |
| Spec 17 reads the member's repo-read key that Spec 14 renames | Spec 17 reads **both** `repo_read_root` and `dev_repo_read_root` |
| Spec 17 decides the default Spec 14 must match | **P0.3**, decided before either branch |
| Spec 16's `finding_class` vs Spec 14's `cited` flags | Spec 16 derives the class from **all** findings and defines empty == empty |
| Spec 16's detector vs Spec 13 lifting the clamp | Spec 16 wires **both** loop chains (`autonomy.py:1408-1429` and `:1759-1780`) |

**Bottom line: after the prep PR, Engineer B can build and merge all four of its
specs without waiting for Engineer A.**

## Suggested merge order

`Phase 0 prep` → **13** (A) → **17 + 18** (B) → **12** (A) → **14** (A) →
**15** (B) → **16** (B).

Rationale: settle the prompt-segment shape (17) before a second spec adds
segments to the same builders; land the finding *producers* (13/14) before the
finding *consumers* (15/16), since both key on what a finding looks like after 14
adds `cited`. 13 first because it is the smallest P0 with the most immediate
effect — the clamp lifts, devs actually fan out, and every later observation is
less confounded by forced serialization. (This inverts the original draft, which
landed 17 last.)

Each spec is one PR into `main`, reviewed before approval.

## What this batch does not fix

The analysis's §5 answer to *"why does this lose to a one-shot Opus 4.8 in Claude
Code?"* is that the council is currently N narrower copies of one model, minus
the feedback loop, plus coordination overhead. This batch closes the feedback
loop (12), the grounding gap (14), and the throughput gap (13). It does **not**
deliver the strongest form of independent verification — a reviewer that
*reproduces* a claim rather than reading about it. That needs an execute surface
for a non-dev role and is deliberately out of scope. Until 12 and 14 land, prefer
a one-shot Claude Code session for anything single-session-sized.

## Cross-cutting implementation notes

- **Prompt goldens.** Specs 12, 15, and 17 touch prompt segments byte-locked by
  `test_prompt_segments_golden.py` (confirmed: it locks all four prompts). Each
  spec states its intended break; keep every new segment **absent** (not empty)
  when its feature is inactive so unaffected projects' goldens do not move.
- **Policy knobs.** Seven new fields, all landed in P0.2. Convention: `max(0, …)`
  where 0 means "disable this detector" (Spec 04 / Spec 10 precedent,
  `autonomy.py:218-231`), `max(1, …)` otherwise.
- **Stop-reason contract.** Only Spec 16 adds one (`revise_livelock`), needing
  **four** edits: the constant (`autonomy.py:51-53`), `FAILURE_STOP_REASONS`
  (`errorta_cli/runstream.py:66-72`), `STOP_REASON_GLOSS` (`:80-102`), and one
  line in `_TERMINAL_BAD` (`render/status.py:26-30`, whose pre-existing gaps are
  Spec 18's to backfill). No `classify_exit` change — `runstream.py:130-146`
  already fails closed on unknown reasons.
- **Prompt-text locks.** Do not "lock" a string a spec exists to change. Specs
  12 and 17 both rewrite prompt segments; the goldens move with them. What stays
  locked is narrower and real: a **gate-less** project's prompts stay
  byte-identical (Spec 12), and the errorta-tool list inside any catalog
  rendering equals `allowed_tools_for_role` (Spec 17).
- **Import direction.** New modules (`gate_bootstrap.py`, `gate_state.py`,
  `capabilities.py`) must not import `runner`; `runner` imports
  `.topology`/`.schemas` at `runner.py:43-45`. Same discipline as
  `coding/paths.py` (F159).
- **Fail-open everywhere.** Every new detection/bootstrap path is additive to a
  working loop; a guarded failure degrades to today's behavior and never breaks a
  merge or a turn.

## Regression locks worth stating up front

Each spec names its own; five are load-bearing for the batch:

- **A bootstrapped acceptance command does not block any merge.** With only
  `acceptance`-scoped commands registered, a reviewer-approved PR still becomes
  mergeable and no tester task is spawned. Without this lock, Spec 12 trades a
  livelock for a deadlock.
- **The merge turn does not run the suite.** No `run_test_commands` call occurs
  inside the merge success block — otherwise Spec 12 cancels Spec 13's throughput.
- **The reddit-look-a-like fixture stays not-foundation-ready** under Spec 13 — a
  bundled JS app with no manifest must not fan out.
- **A gate-less project's prompts stay byte-identical** under Specs 12/15/17.
- **A three-round revise chain with three *distinct* findings still produces
  three revises** under Spec 16 — the breaker must not punish real progress.
