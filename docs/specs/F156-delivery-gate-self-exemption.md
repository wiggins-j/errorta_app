# F156 — Delivery gate: no model self-exemption, no reviewer-less short-circuit

## Problem

Two medium audit findings (G5, G7) where the delivery/merge gate can be satisfied
without real verification. Lower severity than F152–F155 (they need a degenerate
team topology or a lazy/weak model), but they are the same "success without
verification" family and are cheap to close.

### G7 — `no_reviewer` short-circuits tests AND launch, not just the review

`delivery_review` (runner.py:3567-3569):

```python
reviewer_members = members_by_role.get(REVIEWER) or members_by_role.get(PM)
if not reviewer_members:
    return DeliveryReviewResult(passed=True, reason="no_reviewer")
```

This returns **before** step 2 (tests) and step 3 (launch probe). A team with no
REVIEWER *and* no PM therefore reaches `project_done` with **zero** delivery
verification — not even the deterministic test suite or the launch probe run. The
short-circuit is broader than its name: "no reviewer" silently also means "no tests,
no launch check."

### G5 — tester `not_applicable` stamps `tests_passed=True` (model-controlled bypass)

`_execute` tester path (runner.py:3177-3204): a tester turn with
`not_applicable=true` and empty `command_ids` stamps the PR `tests_passed=True` and
makes it mergeable, raising only a **non-blocking** `tests_skipped` alert. A weak or
lazy tester can merge every PR by declaring each slice not-applicable. It is gated to
*empty* command_ids (can't mask a suite that ran and failed), so it is semi-defensible
— but it is a per-PR merge-gate bypass a model fully controls.

## Goal

- **G7:** run the delivery **tests + launch probe regardless of whether a reviewer
  exists**; only the *reviewer verdict* is skipped when there is genuinely no
  reviewer/PM. A reviewer-less team still can't call `done` on an app that doesn't
  build or launch.
- **G5:** make `not_applicable` **bounded and visible** — it cannot be the merge
  path for an unbounded number of PRs in a run, and a run that leans on it surfaces a
  checkpoint/attention signal rather than silently merging everything.

## Design

### G7 — reorder delivery_review

Move the tests + launch steps so they run unconditionally; make only the reviewer
verdict conditional:

```python
reviewer_members = members_by_role.get(REVIEWER) or members_by_role.get(PM)
approved = True                      # default when no reviewer configured
findings = []
if reviewer_members:
    # ... existing reviewer turn: sets approved + findings ...
# tests (step 2) and launch (step 3) ALWAYS run, exactly as today
...
passed = approved and tests_passed and launched_clean
```

So a no-reviewer team gets `approved=True` (it cannot produce a review verdict) but
`tests_passed`/`launched_clean` are still real. `passed` still requires the build/
launch to be clean. The `no_reviewer` early-return is deleted. (The `no_workspace` /
`no_head` vacuous returns are legitimate degenerate cases and stay.)

### G5 — bound not_applicable per run

- Track a run-state counter `tests_not_applicable_count`; each `not_applicable`
  tester turn increments it.
- Keep stamping `tests_passed=True` for that PR (partial slices legitimately have no
  tests), BUT:
  - when the count crosses a small threshold (proposed `not_applicable_soft_limit =
    3`), escalate the `tests_skipped` alert from non-blocking to a **checkpoint /
    attention Problem** so the operator is told "N slices declared no-tests — the
    merge gate is running on review alone," rather than it passing silently.
  - the **delivery review** already re-runs the full registry deterministically at
    the integrated head (runner.py:3638) and — with F154 — a default build gate, so
    the *final* head still can't be not-applicable-gamed. G5's fix is about
    *visibility and a per-run bound*, not the final gate (which F154 covers).

`not_applicable_soft_limit` is configurable via run-setup confirm.

## Non-goals

- Not forbidding `not_applicable` (partial PRs genuinely lack tests) — only bounding
  + surfacing it.
- Not the reviewer≠dev self-approval exclusion (audit G8) — tracked separately as a
  possible later refinement; it needs a topology that assigns one member both roles,
  which the default team builder does not produce.

## Testing

- `test_no_reviewer_still_runs_tests_and_launch` — a team with no REVIEWER/PM: a
  failing launch/build still blocks `done` (was vacuously `passed=True`).
- `test_no_reviewer_clean_app_completes` — no reviewer + clean tests + clean launch →
  `done` (approved defaults True; no regression for the degenerate-but-working case).
- `test_not_applicable_below_limit_merges_quietly` — unchanged for the first few.
- `test_not_applicable_over_limit_raises_attention` — crossing the soft limit
  surfaces a checkpoint/attention Problem.
- Regression: existing delivery-review tests with a reviewer are unchanged.

## Status / sequencing

**Fast-follow** (lower priority than F152/F153/F155). G7 is degenerate (real runs
have a PM); G5 is backstopped by F154's deterministic final build. Specced now for
completeness; implemented after the high-value gaps land. Included in the tightening
PR as a reviewed spec.

## Out of scope

- Reviewer≠dev exclusion (G8), full-registry-per-PR enforcement (G6).
