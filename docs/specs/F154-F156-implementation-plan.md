# F154 + F156 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the last two "success without verification" gaps in the F152 family — give
every test-less project a zero-config compile floor at delivery (F154), and stop the
delivery gate being satisfiable without real verification (F156 G7 + G5).

**Architecture:** Both land in `delivery_review` (`runner.py`). F156-G7 is a reordering:
the `no_reviewer` early-return is deleted and only the *reviewer verdict* becomes
conditional, so tests + launch + web probe always run. F154 then adds a derived
build/typecheck command that runs in step 2 when the test registry is empty, sharing the
launch probe's dependency setup so a missing-deps failure can never be reported as a build
failure. F156-G5 bounds the tester's `not_applicable` escape per run.

**Tech Stack:** Python 3.11+ (`errorta_council.coding`), pytest (`python/tests/coding/`).

## Global Constraints

- **Escape hatches.** `default_build_gate: bool = True` and
  `not_applicable_soft_limit: int = 3` on `CodingAutonomyPolicy`; `False` / `0` reproduce
  today's trace exactly. Both must be added to `policy_to_dict`, `policy_from_dict`, AND
  `docs/coding/PM_REFERENCE.md` — `test_f145_pm_reference` asserts the reference contract
  enumerates every policy field, so omitting the doc is a hard test failure.
- **Deviation from F156's text, stated deliberately:** the spec says
  `not_applicable_soft_limit` is "configurable via run-setup confirm". It goes on
  `CodingAutonomyPolicy` only. `run_setup_fields` is a curated 17-entry operator-facing
  subset of a ~50-field policy; every comparable governance threshold
  (`revise_chain_limit`, `gate_stall_limit`, `plan_streak_limit`) is policy-only, and this
  one guards a degenerate case with a sane default. Adding API surface is not worth it.
- **Fail-closed, but never a phantom finding.** A setup failure or an underivable build is
  `cannot_verify` — it blocks `done` and files NO dev task, exactly as today's launch-setup
  failure does. A build that RAN and failed is a real code finding.
- **No behavior change when the registry is non-empty.** The default build is only derived
  when `store.get_test_commands()` is empty.
- **`None` is always safe.** A stack with no rule in the table yields `None` and preserves
  today's vacuous-clean behavior.

---

### Task 1: F156-G7 — delivery tests + launch run without a reviewer

**Files:**
- Modify: `python/errorta_council/coding/runner.py` (`delivery_review`, ~7332)
- Test: `python/tests/coding/test_f156_delivery_gate.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: no new symbols. `DeliveryReviewResult.reason` gains `"reviewed_no_reviewer"`
  for the reviewer-less-but-verified path; `"no_reviewer"` is no longer emitted.

- [ ] **Step 1: Write the failing tests**

```python
def test_no_reviewer_still_runs_tests_and_launch(...):
    # G7: the early return sat BEFORE steps 2 and 3, so "no reviewer" silently also
    # meant "no tests, no launch check" — a team with neither REVIEWER nor PM reached
    # project_done with zero delivery verification.
    ...  # build a store whose delivered head fails the launch probe, team = DEV only
    result = delivery_review(ledger)
    assert result.passed is False


def test_no_reviewer_clean_app_completes(...):
    # ...and the degenerate-but-working case still completes (approved defaults True).
    result = delivery_review(ledger)
    assert result.passed is True
    assert result.reason == "reviewed_no_reviewer"
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && pytest tests/coding/test_f156_delivery_gate.py -v`
Expected: the first FAILS (`passed is True`, reason `no_reviewer`).

- [ ] **Step 3: Implement**

Replace the early return at `runner.py:7332-7335`:

```python
        # F156 (G7): a missing reviewer skips only the REVIEWER VERDICT — never the
        # deterministic checks. The old early return sat before steps 2 and 3, so
        # "no reviewer" silently also meant "no tests, no launch probe, no web probe",
        # and a team with neither REVIEWER nor PM reached `done` with zero delivery
        # verification. `approved` defaults True because a team that cannot produce a
        # verdict must not be blocked by its absence — but tests/launch/probe below are
        # real, so `passed` still requires the delivered head to build and launch.
        reviewer_members = members_by_role.get(REVIEWER) or members_by_role.get(PM)
        reviewer_member = reviewer_members[0] if reviewer_members else None
```

Then wrap steps 1's body in `if reviewer_member is not None:` with `approved = True`
initialized before it (and `findings: list[dict[str, Any]] = []`). Everything from step 2
onward is unchanged. At the `passed` computation, carry the reason:

```python
        if passed:
            return DeliveryReviewResult(
                passed=True,
                reason="reviewed" if reviewer_member is not None
                       else "reviewed_no_reviewer")
```

**Care:** the `diff` fetch and its `_cannot_verify("delivered diff unavailable")` guard
must stay INSIDE the reviewer branch — with no reviewer there is nothing to diff for, and
failing delivery on an unreadable preview no reviewer would have read is a new false block.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && pytest tests/coding/test_f156_delivery_gate.py -v`
Expected: PASS

- [ ] **Step 5: Run the existing delivery suites**

Run: `cd python && pytest tests/coding/ -q -k "delivery or f146 or f152 or f155"`
Expected: PASS. Any test asserting `reason == "no_reviewer"` must be re-pointed with a
comment naming F156-G7, not deleted.

- [ ] **Step 6: Commit**

```bash
git add python/errorta_council/coding/runner.py python/tests/coding/test_f156_delivery_gate.py
git commit -m "fix(coding): F156 (G7) — a missing reviewer no longer skips tests+launch"
```

---

### Task 2: F156-G5 — bound the tester's not_applicable escape

**Files:**
- Modify: `python/errorta_council/coding/autonomy.py` (`not_applicable_soft_limit`)
- Modify: `python/errorta_council/coding/runner.py` (tester path, ~6904)
- Modify: `docs/coding/PM_REFERENCE.md`
- Test: `python/tests/coding/test_f156_delivery_gate.py`

**Interfaces:**
- Consumes: Task 1's file (same test module).
- Produces: `CodingAutonomyPolicy.not_applicable_soft_limit: int = 3`; run-state counter
  `tests_not_applicable_count: int`.

- [ ] **Step 1: Write the failing tests**

```python
def test_not_applicable_below_limit_merges_quietly(...):
    # Partial PRs genuinely lack tests — the first few are unchanged.
    ...
    assert store.get_run_state().get("tests_not_applicable_count") == 1


def test_not_applicable_over_limit_raises_attention(...):
    # Crossing the soft limit escalates from a deduped non-blocking alert to an
    # operator-visible attention Problem: "N slices declared no-tests — the merge gate
    # is running on review alone."
    ...
```

- [ ] **Step 2: Run to verify they fail**

Expected: `AttributeError: ... 'not_applicable_soft_limit'` / the counter is absent.

- [ ] **Step 3: Implement**

Add the knob to `CodingAutonomyPolicy` with the batch comment convention:

```python
    # F156 (G5): how many PRs in ONE run may merge on a tester `not_applicable`
    # declaration before the run surfaces an operator-visible attention Problem
    # instead of a deduped non-blocking alert. It is NOT a hard cap — a partial slice
    # legitimately has no test, and refusing the declaration would wedge the run. It
    # bounds INVISIBILITY, not the escape itself; the final head is still gated
    # deterministically by delivery_review + F154's default build. 0 disables the
    # escalation entirely, restoring today's always-non-blocking alert.
    not_applicable_soft_limit: int = 3
```

Then in the tester path (`runner.py:6904`), after the existing `record_decision` and
before `raise_tests_skipped_alert`, increment the run-state counter and choose the
severity. Increment inside a guarded try; a counter write failure must never fail the
turn (it degrades to today's non-blocking alert).

Add to `policy_to_dict`, `policy_from_dict` (`max(0, int(...))` — 0 disables, matching the
`gate_stall_limit` convention), and PM_REFERENCE's `autonomy_defaults` + a prose note.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && pytest tests/coding/test_f156_delivery_gate.py tests/coding/test_f145_pm_reference.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/errorta_council/coding/autonomy.py python/errorta_council/coding/runner.py docs/coding/PM_REFERENCE.md python/tests/coding/test_f156_delivery_gate.py
git commit -m "fix(coding): F156 (G5) — bound and surface the not_applicable escape"
```

---

### Task 3: F154 — derive a default verify command

**Files:**
- Modify: `python/errorta_council/coding/runner.py` (new `_default_verify_command`)
- Test: `python/tests/coding/test_f154_default_build_gate.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `_default_verify_command(root: Path) -> tuple[list[str], Path] | None`
  returning `(argv, cwd)`.

The table, verbatim from the spec — keep it small and conservative, since `None` preserves
today's behavior exactly and a wrong rule creates a false gate:

| Detected | Verify command |
|---|---|
| `package.json` with a `build` script | `npm run build` |
| `package.json`, no build script, `tsconfig.json` | `npx --no-install tsc --noEmit` |
| `package.json`, neither | `None` |
| `Cargo.toml` | `cargo build --quiet` |
| `go.mod` | `go build ./...` |
| `pyproject.toml` / `setup.py` / any `*.py` | `python -m compileall -q <root>` |
| none of the above | `None` |

- [ ] **Step 1: Write the failing tests**

```python
def test_derives_npm_build_when_build_script_present(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"build": "vite build"}}')
    argv, cwd = _default_verify_command(tmp_path)
    assert argv == ["npm", "run", "build"] and cwd == tmp_path


def test_derives_tsc_noemit_when_no_build_script(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"dev": "vite"}}')
    (tmp_path / "tsconfig.json").write_text("{}")
    argv, _ = _default_verify_command(tmp_path)
    assert argv == ["npx", "--no-install", "tsc", "--noEmit"]


def test_package_json_without_build_or_tsconfig_is_none(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"dev": "vite"}}')
    assert _default_verify_command(tmp_path) is None


def test_derives_compileall_for_python(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    argv, _ = _default_verify_command(tmp_path)
    assert argv[:4] == ["python", "-m", "compileall", "-q"]


def test_unknown_stack_is_none(tmp_path):
    (tmp_path / "README.md").write_text("hi")
    assert _default_verify_command(tmp_path) is None


def test_malformed_package_json_is_none(tmp_path):
    # Never raise into the delivery gate, and never guess a build for a manifest we
    # could not read.
    (tmp_path / "package.json").write_text("{not json")
    assert _default_verify_command(tmp_path) is None


def test_subdir_manifest_sets_cwd(tmp_path):
    # The CLI delivers into a subdir; cwd must follow the manifest.
    app = tmp_path / "app"; app.mkdir()
    (app / "package.json").write_text('{"scripts": {"build": "next build"}}')
    argv, cwd = _default_verify_command(tmp_path)
    assert argv == ["npm", "run", "build"] and cwd == app
```

- [ ] **Step 2: Run to verify they fail**

Expected: `NameError`/`ImportError` — the resolver does not exist.

- [ ] **Step 3: Implement**

Pure function over the filesystem, fully guarded (any read/parse failure → `None`). Search
the root, then one level of subdirectories, for the manifest — the CLI delivers into a
subdir. Order matters: check Node, then Rust, then Go, then Python, so a polyglot repo gets
its strongest available check.

- [ ] **Step 4: Run tests to verify they pass**
- [ ] **Step 5: Commit**

```bash
git add python/errorta_council/coding/runner.py python/tests/coding/test_f154_default_build_gate.py
git commit -m "feat(coding): F154 — derive a default verify command per stack"
```

---

### Task 4: F154 — run the default build in delivery_review

**Files:**
- Modify: `python/errorta_council/coding/runner.py` (`delivery_review` step 2,
  new `_ensure_delivery_setup`, new `_run_default_build`)
- Modify: `python/errorta_council/coding/autonomy.py` (`default_build_gate`)
- Modify: `docs/coding/PM_REFERENCE.md`, `docs/CLI.md`
- Test: `python/tests/coding/test_f154_default_build_gate.py`

**Interfaces:**
- Consumes: Task 3's `_default_verify_command`.
- Produces: `_ensure_delivery_setup(store, workspace) -> tuple[bool, str]` (ok, detail);
  `_run_default_build(store, workspace, head, should_cancel) -> tuple[bool, bool, str]`
  returning `(built_clean, cannot_verify, detail)` — the same tri-state shape
  `_delivery_launch_evidence` already uses, so the caller's fail-closed handling is
  symmetric.

- [ ] **Step 1: Write the failing tests**

Per the spec's list: `test_node_build_failure_blocks_done`,
`test_node_build_success_allows_done`, `test_python_compileall_catches_syntaxerror`,
`test_no_derivable_build_is_noop`, `test_registered_commands_skip_default_build`,
`test_setup_failure_is_cannot_verify_not_build_fail`, plus
`test_default_build_gate_off_restores_today` for the escape hatch.

- [ ] **Step 2: Run to verify they fail**

- [ ] **Step 3: Implement `_ensure_delivery_setup` (spec §3 option A)**

Setup currently happens inside `RuntimeProcessManager.launch_probe`. Factor the gate so
the build can share it:

```python
def _ensure_delivery_setup(store, workspace) -> tuple[bool, str]:
    """F154 §3 — install deps ONCE before the default build, so a build that fails
    only because dependencies were never installed is reported as cannot_verify
    rather than as a phantom code finding.

    Idempotent by construction: `_setup_pending_venv_missing` is the same gate
    `launch_probe` consults, so calling this first makes the later launch probe's own
    setup a no-op, and skipping it changes nothing. Returns (ok, detail); a project
    with no runnable profile is (True, "") — nothing to set up."""
```

Use `mgr.setup(profile_id)` when `mgr._setup_pending_venv_missing(profile, profile_id)`.
Guard everything; any exception → `(False, detail)` → `cannot_verify`.

- [ ] **Step 4: Implement `_run_default_build` and wire it in**

In `delivery_review` step 2, in the `else` of `if registry:`:

```python
        elif default_build_gate:
            # F154: a greenfield project starts with an EMPTY test registry, so
            # `tests_passed` was unconditionally True and every PR merged into master
            # on a reviewer model-approval with ZERO compilation ever run. F152/F153
            # catch an app that fails to serve or start; they cannot catch a compile
            # error on a path never requested at launch. This is the zero-config
            # compile floor: derived from the stack, run once per delivered head.
            built_clean, build_cannot_verify, build_detail = _run_default_build(
                store, workspace, head, should_cancel=should_cancel)
```

Fold `built_clean` into `passed`, fold `build_cannot_verify` into the same
"do not cache the verdict" condition as `launch_cannot_verify` (so the next completion
claim retries rather than resting on a false negative), and file a **"fix delivery build"**
dev task when `not built_clean and not build_cannot_verify`. A `cannot_verify` records a
decision and files NO task — the launch-error pattern immediately below it.

Run the command through the same sandboxed executor the registry path uses, bound to
`head`, honoring `require_sandbox`.

- [ ] **Step 5: Add the knob + docs**

`default_build_gate: bool = True` on the policy (False restores today's vacuous-clean),
plus `policy_to_dict` / `policy_from_dict` / PM_REFERENCE. Add the `docs/CLI.md` note: a
test-less project gets a default compile/build check at delivery; register real commands
with `errorta test-commands set` to go beyond it.

- [ ] **Step 6: Run tests + the full suite**

Run: `cd python && pytest tests/coding/ -q`
Expected: PASS (~6.5 min; `test_f145_pm_reference` catches an undocumented knob).

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "feat(coding): F154 — default build gate for test-less projects"
```

---

### Task 5: Close out

- [ ] **Step 1:** `ruff check` the touched files (the repo has 9 pre-existing errors in
  `runtime.py`/`testing.py` — do not fix them here, just do not add more).
- [ ] **Step 2:** Full coding suite green.
- [ ] **Step 3:** Update both specs' Status lines from the "spec only / fast-follow" wording
  to `implemented`, noting the stated `not_applicable_soft_limit` deviation.
- [ ] **Step 4:** Commit, then `superpowers:finishing-a-development-branch`.
