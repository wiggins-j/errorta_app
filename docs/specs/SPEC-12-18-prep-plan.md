# Phase 0 — Implementation plan (shared prep PR)

Batch: [SPEC-12-18-gravity-golf-batch-plan.md](SPEC-12-18-gravity-golf-batch-plan.md).

**Owner:** Engineer A · **Branch:** `chore/spec-12-18-prep` · **PR into:** `main`
**Both feature branches base off this once it merges.**

Five behavior-preserving changes whose only purpose is to let two engineers work
in parallel without stepping on each other. **No feature behavior changes in this
PR** — that is the review bar. If a reviewer cannot confirm "nothing behaves
differently", something belongs in a feature branch instead.

## Why this PR exists

Without it, the two branches collide in four places that are certain, not
probable: the review-rejection branch (4 specs, 2 engineers, ~30 lines), the
policy dataclass + its two round-trip functions (all 7 knobs on the same ten
lines), the `dev_repo_read` default (decided by B, consumed by A), and the
prompt-segment goldens. See the batch plan's Phase 0 for the full rationale.

---

## P0.1 — Extract the review-rejection seam

`runner.py:3455-3485` is the reviewer-rejection arm; `runner.py:3641-3653` is its
strict-mode PM-review twin. Both build a `revise:` task from findings.

1. Add `_handle_review_rejection(store, workspace, *, pr, task, findings, source)`
   next to `_reason_from_findings` (`runner.py:472`). Move the **verbatim** body
   of the reviewer arm into it: `update_pr(status="changes_requested")`,
   `_contract_owner_for` (`:2346`), the `revise_depends` assembly, and
   `add_task(title=f"revise: {pr['branch']}", …)`.
2. `source` is `"reviewer"` or `"pm_review"`; the PM twin's only differences are
   its `reason_summary` default (`"PM requested changes"`) and its detail wording
   — keep both behind `source` rather than generalizing the strings.
3. Call it from both sites. No other change.

**Tests.** Extend `test_merge_gate_strict_dual_review.py` /
`test_f141_revise_reason.py`: a reviewer rejection and a PM rejection each still
produce byte-identical task title, `reason_summary`, `detail`, `pr_id`, and
`depends_on` to `main`. Snapshot both before refactoring.

## P0.2 — Land all seven policy fields, no consumers

On `CodingAutonomyPolicy` (`autonomy.py:63`), with round-trip entries in
`policy_to_dict` (`:157`) and `policy_from_dict` (`:183`):

| field | default | clamp in `policy_from_dict` |
|---|---|---|
| `gate_bootstrap` | `True` | `bool(...)` |
| `gate_min_merge_interval` | `3` | `max(1, int(...))` |
| `reviewer_repo_read` | *P0.3* | `bool(...)` |
| `review_min_latency_ms` | `0` | `max(0, int(...))` |
| `review_screenshot` | `False` | `bool(...)` |
| `revise_chain_limit` | `3` | `max(0, int(...))` — 0 disables |
| `revise_livelock_limit` | `5` | `max(0, int(...))` — 0 disables |

Each gets a one-line comment naming its spec, matching the existing style
(`# Spec 04: …`, `autonomy.py:218-231`).

**Tests.** `policy_to_dict(policy_from_dict({})) == policy_to_dict(CodingAutonomyPolicy())`;
each new key round-trips; each `max(0, …)` field accepts 0 and each `max(1, …)`
field clamps 0 up. No behavior test — nothing reads these yet.

## P0.3 — Decide and reconcile `dev_repo_read`

Four statements, two values:

| site | says |
|---|---|
| `autonomy.py:154` (the field) | `False` |
| `autonomy.py:151` (its docstring) | "Default ON" |
| `autonomy.py:230-233` (`policy_from_dict` comment) | "dataclass default (True)" |
| `runner.py:2434` (`build_run_turn` docstring) | "(default True)" |

**Decide the value in this PR** (recommendation: `True` — the mechanism is
verified, the `--tools` allowlist is enforced at the CLI layer, the empty-result
fallback at `async_claude_cli.py:365-380` covers budget exhaustion, and the
docstrings suggest ON was the intent). Then make the field authoritative and
correct all three prose sites. Set `reviewer_repo_read` to the same value.

**Tests.** A drift lock: assert the field default equals the value the docstrings
state (parse or hardcode), and that `dev_repo_read` and `reviewer_repo_read`
agree.

> If the value is `True`, note this is a **live behavior change** for dev turns
> and belongs in its own commit within this PR, called out in the PR body — the
> only exception to the no-behavior-change bar, and it needs an explicit
> reviewer ack.

## P0.4 — `coding/gate_state.py`, the shared read-only seam

New module. Imports `.ledger` and (function-locally) `.evidence` only — **never**
`runner` (`runner` imports `.topology`/`.schemas` at `runner.py:43-45`; F159's
`coding/paths.py` set this precedent).

```python
def gate_available(store) -> bool:
    """Whether anything can produce a gate signal. v1 body is today's
    evidence._tests_required (evidence.py:115-127): registered test commands OR
    a runnable runtime profile. Spec 12 enriches the inputs, not the signature."""

def latest_gate_run(store) -> dict | None:
    """Newest store.list_test_runs() record (ledger.py:1294), or None."""

def latest_gate_text(store, *, cap: int = 4000) -> str:
    """Bounded, verbatim render of latest_gate_run: command ids, status, exit
    code, stdout/stderr previews, and the head it ran against. Empty string when
    there is no run — callers omit the segment entirely rather than emitting an
    empty one."""
```

All three guarded — a ledger hiccup returns the empty/False/None answer.

**Tests.** New `test_gate_state.py`: `gate_available` matches
`evidence._tests_required` on the same fixtures (the equivalence lock, so Spec 12
can later change one without silently diverging); `latest_gate_run` returns the
newest of several; `latest_gate_text` is empty with no runs, contains verbatim
stderr and the head with one, and respects `cap`; every function survives a
raising store.

## P0.5 — De-conflict the prompt goldens

In `test_prompt_segments_golden.py`, make the `_old_*` reference builders **call**
`tool_catalog_text` (already imported, `:38`) and `gate_state.latest_gate_text`
instead of inlining their strings. Spec 12 (A) and Spec 17 (B) then touch
different lines of this file.

**Tests.** The file is its own test — it must still pass unchanged against
`main`'s prompts.

---

## Definition of done

- `pytest python/tests/coding -q` green; `ruff` clean.
- Every P0.1 snapshot matches `main` byte-for-byte.
- PR body lists the P0.3 decision explicitly and flags it as the one behavior
  change (if the value flips).
- Both engineers rebase onto this before starting.
