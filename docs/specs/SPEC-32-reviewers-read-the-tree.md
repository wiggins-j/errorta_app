# Spec 32 — Grounded reviewers read the tree, they don't reject the diff

**Source:** Run 10 (`gravity-golf`, 2026-07-31). Two reviewer-side frictions, both
the same root: a reviewer that has the repo mounted but still reasons from the
diff.

1. The DELIVERY reviewer's one rejection was *"Acceptance test file is truncated in
   diff — cannot verify complete test coverage (test/acceptance.test.js)"* — even
   though SPEC-30 mounts the delivered tree read-only for it. It rejected on a
   diff limitation it could have resolved by OPENING the file, costing a full
   reject → fix → re-review cycle.
2. `reviewer_turn_correction_retry: invalid reviewer intent: input 'tool_calls'
   (expected 'review_verdict')` — the per-PR reviewer tried to emit tool calls (it
   wanted to read files) but the turn schema only accepts a verdict, so the turn
   was rejected and retried. The same "tool hunger" the 2026-07-24 analysis saw as
   leaked `<function_calls>` XML — the model knows it needs to read; the shape
   won't let it.

**Target version:** v0.1 (engine — `runner.py` review prompts, `schemas.py`)
**Depends on:** [SPEC-30](SPEC-30-execution-gate-and-grounded-review.md) (repo
mounting for the delivery + per-PR reviewer)
**Relates to:** the 2026-07-24 run analysis §4.3 (reviewer tool hunger), known-open
#2/#3 (ungrounded reviewer)
**Status:** PARTIAL (verified 2026-08-06 against the code, not the commit log) — Items 1-2 landed; Item 3 (a legal read step) is not
**Landed evidence:** reframed prompts runner.py:3856 (per-PR) / :4066 (delivery); reject_for_truncation = truncated and not repo_read runner.py:3894; live call sites pass a real repo_read (:7104, :7812, :7439)
**NOT landed:** No `read_files` reviewer intent and no bounded pre-verdict read turn. _INTENT_BY_ROLE schemas.py:521 still maps reviewer -> review_verdict only (:580). The implementation FORBIDS the shape in prose (runner.py:3961 "Do NOT emit a tool_plan / tool_calls intent") and leans on the agentic vendor's native Read/Grep — so a tool_calls-shaped reviewer turn still fails validation and still costs a reviewer_turn_correction_retry (runner.py:5861).
**Tests:** tests/coding/test_spec32_reviewer_reads.py (4, Items 1-2). test_pr_reviewer_with_mount_forbids_toolcalls_turn:57 LOCKS the non-implementation of Item 3.

---

## Problem

SPEC-30 gave the reviewer the repo (a read-only worktree), but two things still
push it back onto the diff:

- **The prompt frames the diff as the artifact.** When the diff is truncated, the
  reviewer's instinct is to flag the truncation and reject, rather than open the
  full file from the mount it already has. A truncated *key* file (the acceptance
  test) then produces a spurious rejection and a wasted revise cycle — and in a
  tighter run, that cycle burns the SPEC-23 intervention budget.
- **The turn schema has no read step.** The reviewer can only return a
  `review_verdict`; it cannot say "read these files first, then I will judge." So a
  reviewer that (correctly) wants to inspect a file beyond the diff either
  fabricates a verdict or emits an out-of-shape `tool_calls` intent that is
  rejected and retried — friction, and in the worst case a wrong verdict.

Both are the "enforcement without a satisfiable shape" failure (G1): the reviewer
is told to ground its verdict but the turn makes grounding awkward.

## Principle

> A reviewer with the tree mounted must judge the CODE, not the diff's rendering
> of it. "The diff was truncated" is never a defect in the delivered code — it is a
> cue to open the file. And a reviewer that needs to read before it can rule must
> be able to SAY so in-shape, not be forced to guess or to emit an illegal turn.

## What this spec does

1. **Reframe the review prompts (per-PR and delivery).** When repo_read is on,
   instruct the reviewer explicitly: the diff is a summary; the mounted tree is the
   source of truth; a truncated or elided file must be OPENED and judged from the
   file, never rejected for being truncated. Only reject on a real code defect.
2. **A truncated diff never, by itself, justifies a rejection when the tree is
   mounted.** Generalises the SPEC-30 delivery fix to the per-PR reviewer: with a
   mount available, truncation is a read cue, not a finding.
3. **Give the reviewer a legal read step.** Add a `read_files` reviewer intent (or
   accept a bounded pre-verdict read turn) so "let me open X, Y, then rule" is
   expressible and answered from the mount, instead of a schema-mismatch retry.
   Bounded (one read round per PR head) so it cannot loop; the verdict turn still
   follows and is judged as today.

## Regression locks

1. With repo_read OFF, both prompts and the schema are byte-identical to today
   (the ~50 direct test callers and the no-mount path are unchanged).
2. A genuine code defect still produces a rejection; this spec only removes the
   *truncation-as-defect* and *no-read-step* frictions.
3. The read step is bounded (one round per head) — it cannot become a way to idle;
   the per-role satisfiability test (SPEC-25 S5) covers the new intent.
4. Prompt goldens move only by the intended segment (the P0.4 golden-lock pattern).

## Definition of done

- A delivery or per-PR reviewer with the tree mounted does NOT reject because a
  file is truncated in the diff — it opens the file. Locked by a test that feeds a
  truncated diff + a mounted tree and asserts no truncation-only rejection.
- A reviewer that needs to read first can express it in one legal turn; a
  `tool_calls`-shaped reviewer turn no longer costs a correction retry.
