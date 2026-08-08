# SPEC-46 — per-task drop-count quarantine: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop one repeatedly-dropped task from halting the whole run via `planning_churn`. Count drops per normalized task identity; at a threshold, quarantine that identity (stop re-creating it) and escalate it to a deduped operator Problem, while the rest of the backlog keeps running.

**Architecture:** A run-scoped **drop ledger** (`identity_key → count`) persisted in `run_state.json`. `_apply_pm_cancels` increments the count and records the machine-readable drop reason at the drop. `_materialize_pm_tasks` looks up the count before creating a task and suppresses creation once it reaches `task_drop_quarantine_limit` (default 3, deliberately `< plan_streak_limit` 6), raising a deduped `task_pathology` Problem instead. A new terminal reason `quarantined_task_needs_input` reports the case where a quarantined task is the only remaining work, so the operator sees the real cause instead of `planning_churn`.

**Tech Stack:** Python 3, `errorta_council.coding`, `pytest`, `rich`. Depends on the `drop_reasons` module and reason-surfacing renderers from [SPEC-45](SPEC-45-plan.md); if SPEC-46 lands first, pull Task 1 of the SPEC-45 plan (the vocabulary module + decisions/board rendering) in ahead of this plan's Task 2.

## Global Constraints

- **`task_drop_quarantine_limit` (default 3) MUST be `< plan_streak_limit` (default 6)** so quarantine fires before `planning_churn`. This ordering is a regression lock (Task 3).
- **Identity key = the dedupe normalization**, not the task id. Re-created tasks are new records; the counter keys on normalized filler-stripped title tokens + declared `target_files` so a count survives create→drop→re-create cycles. Expose it as `task_dedupe.identity_key` so the ledger and dedupe cannot diverge.
- The drop ledger is **per-run** — persisted in `run_state.json` via `store.set_run_state`/`get_run_state` (`ledger.py:1469-1498`). A fresh run starts clean. No cross-run persistence.
- The dedupe exemption for `dropped`/`blocked` (`task_dedupe.OPEN_STATES`, `task_dedupe.py:29`) is **untouched** — damping is the added pairing, not a change to the exemption.
- **Best-effort, never raise into the loop**; additive JSON only, no schema migration.
- Tests: `LedgerStore(name, root=tmp_path)` then `create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)`.

---

### Task 1: `task_dedupe.identity_key` — the shared normalized identity

**Files:**
- Modify: `python/errorta_council/coding/task_dedupe.py` (add `identity_key`)
- Test: `python/tests/coding/test_task_dedupe.py`

**Interfaces:**
- Produces: `task_dedupe.identity_key(*, title: str, paths: Iterable[str]) -> str` — a stable string derived from the filler-stripped title token set (sorted) and normalized target paths (sorted). Two proposals that dedupe as the same job produce the same key; a numeric distinguisher or a different file set produces a different key.
- Consumed by: the drop ledger (Task 2) and the quarantine check (Task 4).

- [ ] **Step 1: Write the failing test**

```python
# append to python/tests/coding/test_task_dedupe.py
def test_identity_key_matches_across_filler_verb_swap():
    a = task_dedupe.identity_key(title="Fix the parser harness", paths=[])
    b = task_dedupe.identity_key(title="Implement the parser harness", paths=[])
    assert a == b  # filler verbs (fix/implement) are stripped -> same identity


def test_identity_key_distinguishes_paths_and_numbers():
    base = task_dedupe.identity_key(title="add pagination", paths=["a.py"])
    other_path = task_dedupe.identity_key(title="add pagination", paths=["b.py"])
    numbered = task_dedupe.identity_key(title="fix level 50", paths=[])
    numbered2 = task_dedupe.identity_key(title="fix level 60", paths=[])
    assert base != other_path
    assert numbered != numbered2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest tests/coding/test_task_dedupe.py::test_identity_key_matches_across_filler_verb_swap -v`
Expected: FAIL — `AttributeError: module 'task_dedupe' has no attribute 'identity_key'`

- [ ] **Step 3: Implement `identity_key`**

Add to `task_dedupe.py`, reusing the existing `normalized_tokens` and `normalized_target_paths`:

```python
def identity_key(*, title: str, paths: Iterable[str]) -> str:
    """A stable identity for the drop ledger (SPEC-46), keyed on the SAME
    normalization dedupe uses so the two can never diverge. Filler verbs are
    stripped; declared paths and digit-bearing tokens (a level/version/count)
    stay significant, so `fix level 50` and `fix level 60` are distinct."""
    tokens = "|".join(sorted(normalized_tokens(title)))
    path_set = "|".join(sorted(normalized_target_paths(paths)))
    return f"{tokens}##{path_set}"
```

Note: `identity_key` is intentionally coarser than `find_duplicate` — it does not model the 0.8 Jaccard fuzz, only exact normalized-token equality. That is correct for a drop counter: a task the PM keeps re-dropping restates itself near-verbatim, and a coarse key that occasionally splits one pathological identity into two is strictly safer than one that merges two distinct tasks into a shared quarantine.

- [ ] **Step 4: Run tests**

Run: `cd python && python -m pytest tests/coding/test_task_dedupe.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/errorta_council/coding/task_dedupe.py python/tests/coding/test_task_dedupe.py
git commit -m "feat(coding): SPEC-46 — task_dedupe.identity_key for the drop ledger"
```

---

### Task 2: Drop ledger + reason on `pm_task_cancelled`

Increment a per-identity drop count in `run_state.json` and stamp the machine-readable reason at the drop.

**Files:**
- Create: `python/errorta_council/coding/drop_ledger.py`
- Modify: `python/errorta_council/coding/runner.py:3706-3712` (the `_apply_pm_cancels` drop record)
- Test: `python/tests/coding/test_spec46_drop_quarantine.py`

**Interfaces:**
- Produces: `drop_ledger.record_drop(store, identity: str) -> int` (returns the new count); `drop_ledger.drop_count(store, identity: str) -> int`; both read/write `run_state["drop_ledger"]` (a `{identity: int}` dict).
- Consumes: `store.get_run_state`/`set_run_state`; `task_dedupe.identity_key` (Task 1); `drop_reasons` (SPEC-45 Task 1, or pulled ahead).

- [ ] **Step 1: Write the failing test**

```python
# python/tests/coding/test_spec46_drop_quarantine.py
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import drop_ledger, task_dedupe
from errorta_council.coding.ledger import LedgerStore


def _store(tmp_path: Path, name: str = "p46") -> LedgerStore:
    s = LedgerStore(name, root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def test_drop_ledger_counts_per_identity(tmp_path):
    store = _store(tmp_path)
    key = task_dedupe.identity_key(title="wire the integration layer", paths=[])
    assert drop_ledger.drop_count(store, key) == 0
    assert drop_ledger.record_drop(store, key) == 1
    assert drop_ledger.record_drop(store, key) == 2
    assert drop_ledger.drop_count(store, key) == 2
    # A different identity is independent.
    other = task_dedupe.identity_key(title="add a config flag", paths=[])
    assert drop_ledger.drop_count(store, other) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/coding/test_spec46_drop_quarantine.py::test_drop_ledger_counts_per_identity -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'errorta_council.coding.drop_ledger'`

- [ ] **Step 3: Implement the drop ledger**

```python
# python/errorta_council/coding/drop_ledger.py
"""SPEC-46 — the per-run drop counter, keyed by normalized task identity.

Because dedupe deliberately lets a `dropped` task be re-created as a NEW record
(so a regression can re-open one), a per-task-id counter cannot see a create→drop
→re-create loop. This ledger keys on `task_dedupe.identity_key`, persisted in
`run_state.json`, so a repeatedly-dropped job accumulates a count across cycles.
"""
from __future__ import annotations

from typing import Any

_KEY = "drop_ledger"


def _ledger(store: Any) -> dict[str, int]:
    try:
        raw = store.get_run_state().get(_KEY) or {}
        return {str(k): int(v) for k, v in raw.items()}
    except Exception:  # noqa: BLE001 — a read failure means "no counts yet"
        return {}


def drop_count(store: Any, identity: str) -> int:
    return _ledger(store).get(str(identity), 0)


def record_drop(store: Any, identity: str) -> int:
    """Increment and persist the count for `identity`; return the new value."""
    led = _ledger(store)
    led[str(identity)] = led.get(str(identity), 0) + 1
    try:
        store.set_run_state(**{_KEY: led})
    except Exception:  # noqa: BLE001 — best-effort; a lost increment only delays quarantine
        pass
    return led[str(identity)]
```

- [ ] **Step 4: Increment at the drop + stamp the reason**

In `_apply_pm_cancels` (`runner.py:3706-3713`), replace the `store.update_task` + `store.record_decision` block so it computes the identity, records the drop, and writes the reason blob. Add imports at the top of `runner.py`: `from . import drop_ledger as _drop_ledger` and (if not already added for SPEC-45) `from . import drop_reasons as _drop_reasons`.

```python
        try:
            paths = _declared_target_paths(getattr(task, "title", ""),
                                           getattr(task, "detail", ""))
            identity = task_dedupe.identity_key(
                title=getattr(task, "title", ""), paths=paths)
            n = _drop_ledger.record_drop(store, identity)
            store.update_task(tid, state="dropped",
                              reason_summary="PM pruned this over-scoped task")
            store.record_decision(
                title=f"PM dropped task: {getattr(task, 'title', tid)}",
                context="pm_cancel", choice="pm_task_cancelled",
                rationale="the PM pruned this obsolete / over-scoped task to converge",
                related_task_ids=[tid],
                extra={
                    "drop_count": n,
                    **_drop_reasons.reason_blob(
                        _drop_reasons.PM_PRUNED,
                        detail=f"dropped {n}× this run"),
                })
            dropped.append(tid)
        except Exception:  # noqa: BLE001 — best-effort prune
            pass
```

`_declared_target_paths` is the same helper `_materialize_pm_tasks` uses (`runner.py:3365`); confirm its name/signature at implement time and reuse it so the ledger key matches what materialization will compute.

- [ ] **Step 5: Add a test that the drop decision carries the reason + count**

```python
# append to test_spec46_drop_quarantine.py
class _Intent:
    def __init__(self, cancel_task_ids):
        self.cancel_task_ids = cancel_task_ids


def test_drop_decision_carries_reason_and_count(tmp_path):
    from errorta_council.coding.runner import _apply_pm_cancels
    store = _store(tmp_path)
    t = store.add_task(title="wire the integration layer", role="dev")
    _apply_pm_cancels(store, _Intent([t.task_id]))
    dec = next(d for d in store.list_decisions()
               if d.get("choice") == "pm_task_cancelled")
    assert dec["reason_code"] == "pm_pruned"
    assert dec["drop_count"] == 1
```

- [ ] **Step 6: Run tests**

Run: `cd python && python -m pytest tests/coding/test_spec46_drop_quarantine.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add python/errorta_council/coding/drop_ledger.py python/errorta_council/coding/runner.py python/tests/coding/test_spec46_drop_quarantine.py
git commit -m "feat(coding): SPEC-46 — per-identity drop ledger + machine-readable drop reason"
```

---

### Task 3: The `task_drop_quarantine_limit` policy knob

**Files:**
- Modify: `python/errorta_council/coding/autonomy.py:219` (add the field next to `plan_streak_limit`)
- Test: `python/tests/coding/test_spec46_drop_quarantine.py`

**Interfaces:**
- Produces: `CodingAutonomyPolicy.task_drop_quarantine_limit: int = 3`.

- [ ] **Step 1: Write the failing test (also locks the ordering invariant)**

```python
# append to test_spec46_drop_quarantine.py
def test_quarantine_limit_default_and_ordering():
    from errorta_council.coding.autonomy import CodingAutonomyPolicy
    p = CodingAutonomyPolicy()
    assert p.task_drop_quarantine_limit == 3
    # MUST fire before planning_churn, or the whole run halts first.
    assert p.task_drop_quarantine_limit < p.plan_streak_limit
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/coding/test_spec46_drop_quarantine.py::test_quarantine_limit_default_and_ordering -v`
Expected: FAIL — `AttributeError: 'CodingAutonomyPolicy' object has no attribute 'task_drop_quarantine_limit'`

- [ ] **Step 3: Add the policy field**

In `autonomy.py`, immediately after the `plan_streak_limit: int = 6` field (`autonomy.py:219`), add:

```python
    # SPEC-46: after a task's normalized identity has been created-and-dropped this
    # many times in one run, quarantine it (stop re-creating it) and raise a deduped
    # operator Problem, instead of letting the create↔drop loop climb the plan streak
    # to `planning_churn` and halt the whole run. MUST stay < plan_streak_limit so
    # quarantine fires first. 0 disables the damping (planning_churn is then the only
    # backstop, i.e. today's behaviour).
    task_drop_quarantine_limit: int = 3
```

- [ ] **Step 4: Run tests**

Run: `cd python && python -m pytest tests/coding/test_spec46_drop_quarantine.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/errorta_council/coding/autonomy.py python/tests/coding/test_spec46_drop_quarantine.py
git commit -m "feat(coding): SPEC-46 — task_drop_quarantine_limit policy knob (default 3)"
```

---

### Task 4: Quarantine at threshold — suppress the re-create + raise a deduped Problem

**Files:**
- Modify: `python/errorta_council/coding/runner.py:3378-3396` (before the dedupe check in `_materialize_pm_tasks`)
- Create: `python/errorta_council/coding/attention.py` addition — `raise_task_pathology_problem`
- Test: `python/tests/coding/test_spec46_drop_quarantine.py`

**Interfaces:**
- Produces: `attention.raise_task_pathology_problem(project_id, *, identity: str, title: str, drops: int, reason_code: str, store=None) -> AttentionSignal | None` — deduped by `(source="task_pathology", context.identity)`; returns `None` if one is already open for that identity.
- Consumes: `drop_ledger.drop_count`, `task_dedupe.identity_key`, the policy knob (Task 3).

- [ ] **Step 1: Write the failing test for the escalation raiser (deduped)**

```python
# append to test_spec46_drop_quarantine.py
def test_task_pathology_problem_is_deduped(tmp_path):
    from errorta_council.coding import attention
    store = _store(tmp_path)
    first = attention.raise_task_pathology_problem(
        store.project_id, identity="k1", title="wire the integration layer",
        drops=3, reason_code="pm_pruned", store=store)
    assert first is not None
    dupe = attention.raise_task_pathology_problem(
        store.project_id, identity="k1", title="wire the integration layer",
        drops=4, reason_code="pm_pruned", store=store)
    assert dupe is None  # one open Problem per identity, no stacking
    opens = [s for s in attention.list_open(store.project_id, store=store)
             if s.source == "task_pathology"]
    assert len(opens) == 1
    assert "pm_pruned" in opens[0].summary or "pm_pruned" in (opens[0].pm_evaluation or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/coding/test_spec46_drop_quarantine.py::test_task_pathology_problem_is_deduped -v`
Expected: FAIL — `AttributeError: module 'attention' has no attribute 'raise_task_pathology_problem'`

- [ ] **Step 3: Implement the deduped Problem raiser**

Add to `attention.py`, mirroring `raise_monitor_problem` (`attention.py:365-390`) but deduped on `context.identity`:

```python
def raise_task_pathology_problem(
    project_id: str, *, identity: str, title: str, drops: int, reason_code: str,
    stage: str = "development", store: LedgerStore | None = None,
) -> AttentionSignal | None:
    """SPEC-46: a task repeatedly created-and-dropped has been quarantined. Raise a
    blocking Problem so the operator can see it needs input, deduped by the task's
    normalized identity so a loop re-encountering it does not stack alarms. It flags
    for the operator; it does not stop dispatch of other backlog."""
    store = store or _store(project_id)
    for s in list_open(project_id, store=store):
        if (s.kind == "problem" and s.source == "task_pathology"
                and str((s.context or {}).get("identity") or "") == str(identity)):
            return None
    pm_eval = (
        f"'{title}' was created and dropped {drops}× this run (reason: {reason_code}) "
        "and has been quarantined so the run can continue. It needs operator input — "
        "re-scope it, supply the missing prerequisite, or drop it for good.")
    return raise_signal(
        project_id, kind="problem", source="task_pathology", stage=stage,
        title=f"quarantined: {title}"[:80],
        summary=f"dropped {drops}× — reason: {reason_code}; needs operator input",
        pm_evaluation=pm_eval, suggestions=list(_MONITOR_SUGGESTIONS),
        context={"identity": str(identity), "drops": int(drops),
                 "reason_code": reason_code},
        store=store,
    )
```

- [ ] **Step 4: Write the failing integration test for quarantine suppression**

```python
# append to test_spec46_drop_quarantine.py
class _Planned:
    def __init__(self, title, detail=""):
        self.title = title; self.detail = detail; self.depends_on = []
        self.task_type = "implementation"; self.difficulty_tier = "mid"
        self.preferred_member_id = ""; self.preferred_route_id = ""
        self.assignment_rationale = ""


class _PlanIntent:
    def __init__(self, tasks):
        self.tasks = tasks


def test_task_is_quarantined_at_threshold(tmp_path):
    from errorta_council.coding import drop_ledger, task_dedupe, attention
    from errorta_council.coding.runner import _materialize_pm_tasks
    store = _store(tmp_path)
    title = "consolidate the module registry"
    key = task_dedupe.identity_key(title=title, paths=[])
    # Simulate 3 prior drops of this identity.
    for _ in range(3):
        drop_ledger.record_drop(store, key)
    created = _materialize_pm_tasks(store, _PlanIntent([_Planned(title)]))
    # At the limit, the task is NOT created; a quarantine decision + Problem exist.
    assert created == []
    assert any(d.get("choice") == "task_quarantined" for d in store.list_decisions())
    opens = [s for s in attention.list_open(store.project_id, store=store)
             if s.source == "task_pathology"]
    assert len(opens) == 1
```

- [ ] **Step 5: Implement the quarantine gate in `_materialize_pm_tasks`**

In the `for planned in intent.tasks:` loop, *before* the dedupe `find_duplicate` call (`runner.py:3378`), insert the quarantine check. `paths` is already computed one line above at `runner.py:3365`:

```python
        identity = task_dedupe.identity_key(title=planned.title, paths=paths)
        quarantine_limit = getattr(_active_policy, "task_drop_quarantine_limit", 3)
        if quarantine_limit and _drop_ledger.drop_count(store, identity) >= quarantine_limit:
            title_to_id[planned.title] = ""
            store.record_decision(
                title=f"task quarantined (dropped repeatedly): {planned.title}",
                context="drop_quarantine", choice="task_quarantined",
                rationale=(f"{planned.title!r} has been created and dropped "
                           f"{_drop_ledger.drop_count(store, identity)}× this run; "
                           "quarantined so the run continues on the rest of the backlog"),
                extra={
                    "drop_count": _drop_ledger.drop_count(store, identity),
                    **_drop_reasons.reason_blob(
                        _drop_reasons.OVER_SCOPED,
                        detail=f"quarantined after {quarantine_limit} drops"),
                })
            attention.raise_task_pathology_problem(
                store.project_id, identity=identity, title=planned.title,
                drops=_drop_ledger.drop_count(store, identity),
                reason_code=_drop_reasons.OVER_SCOPED, store=store)
            continue
```

Determine how `_materialize_pm_tasks` can see the active policy for `quarantine_limit`. It currently takes `(store, intent, *, parent_task)` and does not receive the policy. Two acceptable routes — pick the one matching the surrounding code:
  - **Preferred:** read the persisted policy the same way the route does — `from .autonomy import policy_from_store` (or the existing helper the codebase uses to load `CodingAutonomyPolicy` from the ledger/`autonomy.json`); call it inside `_materialize_pm_tasks` and use `.task_drop_quarantine_limit`. Confirm the loader's name against `autonomy.py`.
  - **Fallback:** thread a `quarantine_limit: int = 3` keyword param into `_materialize_pm_tasks` from its two call sites (`runner.py:6496`, `runner.py:6623`), which do have the policy in scope.
Replace the `_active_policy` placeholder above with whichever you choose; do not leave `_active_policy` undefined.

- [ ] **Step 6: Run tests**

Run: `cd python && python -m pytest tests/coding/test_spec46_drop_quarantine.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add python/errorta_council/coding/runner.py python/errorta_council/coding/attention.py python/tests/coding/test_spec46_drop_quarantine.py
git commit -m "feat(coding): SPEC-46 — quarantine repeatedly-dropped tasks + deduped task_pathology Problem"
```

---

### Task 5: `quarantined_task_needs_input` terminal reason + status rollup

Give the operator a truthful terminal reason when a quarantined task is the only work left, instead of `planning_churn`/exit 7, and a "N quarantined" status line.

**Files:**
- Modify: `python/errorta_cli/runstream.py:66-105` (`SUCCESS_STOP_REASONS`/`STOP_REASON_GLOSS`)
- Modify: `python/errorta_council/coding/autonomy.py` (emit the reason from the churn detector when the only non-terminal work is quarantined) — see Step 3 for the exact placement
- Modify: `python/errorta_app/routes/coding.py:2900-2915` (`_run_backlog_shape` — add `quarantined`)
- Modify: `python/errorta_cli/render/status.py:172-180` (render the rollup)
- Test: `python/tests/coding/test_spec46_drop_quarantine.py`, `python/tests/cli/test_runstream_exit.py` (or the existing runstream classification test)

**Interfaces:**
- Produces: stop reason literal `"quarantined_task_needs_input"` classified benign (exit 0) with a gloss; `_run_backlog_shape` returns `…, "quarantined": Q`.

- [ ] **Step 1: Write the failing classification test**

```python
# append to test_spec46_drop_quarantine.py
def test_quarantine_stop_reason_is_benign_exit():
    from errorta_cli import runstream
    payload = {"running": False,
               "state": {"status": "stopped",
                         "stop_reason": "quarantined_task_needs_input"}}
    assert runstream.classify_exit(payload) == runstream.EXIT_OK
    assert "quarantine" in runstream.gloss("quarantined_task_needs_input").lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/coding/test_spec46_drop_quarantine.py::test_quarantine_stop_reason_is_benign_exit -v`
Expected: FAIL — the reason is unknown, so `classify_exit` fails closed to `EXIT_RUN_FAILED` and the gloss is the raw string.

- [ ] **Step 3: Register the reason**

In `runstream.py`, add `"quarantined_task_needs_input"` to `SUCCESS_STOP_REASONS` (`:75-77`) and a gloss in `STOP_REASON_GLOSS` (`:80`):

```python
    "quarantined_task_needs_input": (
        "a task was quarantined after repeated drops and is the only work left — "
        "resolve it in the attention list, then continue"),
```

Rationale for benign (exit 0): the run correctly isolated the pathological task and raised an actionable operator Problem; it is a pause for input, not a failure. (If a headless-CI caller later needs a non-zero here, add a dedicated exit code rather than reclassifying — noted as a follow-up.)

Then, at the point where the churn/idle detector would return `planning_churn` (or where the loop concludes there is nothing dispatchable), add a guard: if every non-terminal task is quarantined-or-blocked and at least one open `task_pathology` Problem exists, return `quarantined_task_needs_input` instead. Read `_account_planning_churn` (`autonomy.py:2848-2879`) and the loop's stop-reason assembly first; place the guard where the reason is chosen, keeping `planning_churn` for the generic case. Add a focused test in `test_planning_churn.py` that a single quarantined task yields `quarantined_task_needs_input`, not `planning_churn`.

- [ ] **Step 4: Add the quarantined count to `_run_backlog_shape` + render it**

In `routes/coding.py` `_run_backlog_shape`, count open `task_pathology` Problems (the quarantined set) and add it to the return dict:

```python
        try:
            from errorta_council.coding import attention
            quarantined = sum(
                1 for s in attention.list_open(store.project_id, store=store)
                if s.source == "task_pathology")
        except Exception:  # noqa: BLE001
            quarantined = 0
        return {"todo": todo_n, "dispatchable": len(seen),
                "quarantined": quarantined}
```

(If SPEC-45's Task 5 already added `blocked_on_capability` here, keep both keys.) In `status.py`, after the `todo:` line, add:

```python
        quarantined = backlog.get("quarantined")
        if quarantined:
            lines.append(Text(
                f"quarantined: {quarantined} task(s) need operator input (see attention)",
                style="cli.bad"))
```

- [ ] **Step 5: Run tests**

Run: `cd python && python -m pytest tests/coding/test_spec46_drop_quarantine.py tests/coding/test_planning_churn.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add python/errorta_cli/runstream.py python/errorta_council/coding/autonomy.py python/errorta_app/routes/coding.py python/errorta_cli/render/status.py python/tests/coding/test_spec46_drop_quarantine.py python/tests/coding/test_planning_churn.py
git commit -m "feat: SPEC-46 — quarantined_task_needs_input terminal reason + status rollup"
```

---

### Task 6: End-to-end — the loop keeps running instead of halting on planning_churn

The behavioral proof the issue is about: a pathological task quarantines while a self-contained task completes.

**Files:**
- Test: `python/tests/coding/test_spec46_drop_quarantine.py`

- [ ] **Step 1: Write the end-to-end test**

```python
# append to test_spec46_drop_quarantine.py
def test_quarantine_isolates_one_task_backlog_continues(tmp_path):
    """A task dropped to the threshold is suppressed on the next materialize while a
    fresh, distinct task still gets created — the loop is not wedged on the bad one."""
    from errorta_council.coding import drop_ledger, task_dedupe
    from errorta_council.coding.runner import _materialize_pm_tasks
    store = _store(tmp_path)
    bad = "reconcile the cross-module event bus"
    key = task_dedupe.identity_key(title=bad, paths=[])
    for _ in range(3):
        drop_ledger.record_drop(store, key)
    created = _materialize_pm_tasks(store, _PlanIntent([
        _Planned(bad), _Planned("add a --verbose flag to the CLI")]))
    titles = [t.title for t in created]
    assert bad not in titles                       # quarantined
    assert "add a --verbose flag to the CLI" in titles  # unrelated work proceeds
```

- [ ] **Step 2: Run it**

Run: `cd python && python -m pytest tests/coding/test_spec46_drop_quarantine.py::test_quarantine_isolates_one_task_backlog_continues -v`
Expected: PASS

- [ ] **Step 3: Full coding suite**

Run: `cd python && python -m pytest tests/coding -q`
Expected: PASS. In particular confirm `test_planning_churn.py` still trips `planning_churn` for a genuine plan-only loop that is NOT a single quarantined task (the guard must be narrow).

- [ ] **Step 4: Flip the spec Status + commit**

Edit `docs/specs/SPEC-46-drop-count-quarantine.md`: `**Status:** proposed` → `landed (merged in PR #…)`.

```bash
git add docs/specs/SPEC-46-drop-count-quarantine.md
git commit -m "docs(spec): SPEC-46 landed"
```

---

## Self-Review

- **Spec coverage:** identity-keyed drop ledger → Tasks 1–2; increment at drop + reason → Task 2; policy knob `< plan_streak_limit` → Task 3 (+ ordering lock); quarantine suppression → Task 4; deduped `task_pathology` Problem → Task 4; distinct `quarantined_task_needs_input` terminal → Task 5; "keep executing the rest of the backlog" → Task 6; dedupe exemption untouched → no task modifies `OPEN_STATES`. All map.
- **Type consistency:** `task_dedupe.identity_key(*, title, paths) -> str`, `drop_ledger.record_drop(store, identity) -> int` / `drop_count(store, identity) -> int`, and `attention.raise_task_pathology_problem(project_id, *, identity, title, drops, reason_code, store=None)` are used with identical signatures across Tasks 2/4/5/6.
- **Placeholder scan:** the two spots requiring a name confirmed against the code are called out explicitly — `_declared_target_paths` (reused, Task 2 Step 4) and the policy-loader / param-threading choice for the quarantine limit (Task 4 Step 5). Neither is a silent TODO; each names the concrete alternatives.
- **Dependency on SPEC-45:** `drop_reasons` (SPEC-45 Task 1) and the decisions/board reason rendering are prerequisites; the header states the pull-ahead if SPEC-46 lands first.
