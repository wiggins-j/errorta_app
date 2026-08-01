# SPEC-35 — implementation & writing plan

Companion to [SPEC-35](SPEC-35-recoverable-acceptance-done-gate.md). This is the
order of work, the exact code surface, the tests that lock each part, and the
review/merge flow. Scope is small and concentrated in three files — no parallel
edits, so it is implemented sequentially and then adversarially reviewed (the SPEC-34
flow that caught a real blocker before merge).

## Guiding constraint (from the SPEC-34 review)

Every block must clear itself. The plan is ordered so the **recovery path exists
before the block is wired** — G1/G2 (read the live result) land first, G3 (block)
only after, and the recovery test (G3's red→green) is written *with* the block, not
after.

## Phase 1 — G1: precise acceptance-result lookup (`gate_state.py`)

- Add `latest_acceptance_result(store) -> dict | None` returning `{"passed": bool,
  "head": str}` for the newest `list_test_runs()` session whose per-command `results`
  carry a command_id that is `scope == "acceptance"` in `get_test_commands()`.
  Ignore probe/unit/PR runs. READ-ONLY, guarded, returns `None` on any miss.
- Add it to `__all__`; keep the SPEC-12-prep import discipline (no `runner` import).
- **Tests** (`test_spec35_acceptance_gate.py::G1`): mixed `list_test_runs`
  (web:probe run, unit run, acceptance run) → returns the acceptance one; acceptance
  scope resolved from the live registry; `None` when no acceptance command registered;
  guarded against a raising `list_test_runs`.

## Phase 2 — G2: the status classifier (`completion.py`)

- Add `acceptance_gate_status(store, current_head) -> Literal["no_gate","green",
  "red","stale"]` — pure, read-only, fail-open to `no_gate` on any read error (never
  invents a block). Uses G1 + `get_test_commands()` scope check.
- Export it; keep `completion.py` pure/READ-ONLY (its module contract).
- **Tests** (`::G2`): each of the four verdicts from constructed state; head match vs
  mismatch → green vs stale; registered-but-no-run → stale; unregistered → no_gate;
  read error → no_gate (fail-open).

## Phase 3 — G3: block + recover at both `done` chokepoints (`runner.py`)

- Add `_acceptance_gate_blocks_done(store, head) -> Optional[str]` (reason to block, or
  None). `red` → reason string. `stale` → arm the in-loop gate (`set_run_state(
  gate_due=True, gate_dirty_head=head)`) and return a reason. `green`/`no_gate` → None.
  Resolve `head` from the current master head (`workspace.head()` / run_state).
- Wire it at BOTH done paths, right after the `pending_completion_work` refusal:
  - plan turn (~runner.py:6001): on block, record `pm_completion_refused` + return
    `completion_refused` (reason `acceptance_gate_red` / `acceptance_gate_stale`).
  - last-word turn (~runner.py:5882): on block, record + return the `noop` refusal.
- Keep the existing `_ack_unrun_acceptance_test` + `_record_completion_oracles` on the
  allow path unchanged.
- **Tests** (`::G3`): green → allowed; red → refused; stale → arms `gate_due`/
  `gate_dirty_head` + refused; no_gate → allowed. Both chokepoints.

## Phase 4 — G4: bound the stale loop (`runner.py`)

- Counter `acceptance_gate_stale_arms` in run_state; increment on each `stale`
  arm-refuse; reset on any fresh result at head. Past `N` (const, ~3) with no fresh
  result → stop arming, degrade to the SPEC-34 non-blocking ack, and raise a
  `completion_blocked` Problem (human-routed).
- **Tests** (`::G4`): N stale cycles with no result → degrades to ack +
  `completion_blocked`, does NOT arm again; a fresh result before N resets the counter.

## Phase 5 — the recovery + regression locks

- **Recovery end-to-end** (`::recovery`): drive red → (simulate fix/merge + a green
  in-loop acceptance run at the new head) → status flips `green` → `done` allowed,
  with NO manual run-state clearing. This is the property SPEC-34's draft lacked.
- **Isolation** (`::isolation`): a green web:probe / unit / PR run at head does NOT
  make the acceptance gate green.
- **No-wedge floor**: no acceptance command → `no_gate` → never blocked.

## Phase 6 — review, fix, merge

1. `ruff` + full `tests/coding/` suite green.
2. Branch `feat/spec-35-recoverable-done-gate`; commit per phase or as one reviewed
   change.
3. **Adversarial multi-agent review** of the diff (correctness/wedge-safety,
   spec-fidelity vs SPEC-35's recovery invariant + regression locks, regression risk)
   → per-finding verification, exactly as for SPEC-34. The wedge-safety lens is
   mandatory here: prove no path blocks `done` without a self-clearing recovery.
4. Apply confirmed findings, re-run the suite.
5. Open PR into `main` with the red→green recovery test called out as the safety
   proof; merge on green.

## Files touched

| File | Change |
|---|---|
| `python/errorta_council/coding/gate_state.py` | + `latest_acceptance_result` (G1) |
| `python/errorta_council/coding/completion.py` | + `acceptance_gate_status` (G2) |
| `python/errorta_council/coding/runner.py` | + `_acceptance_gate_blocks_done` + wiring at both done chokepoints (G3, G4) |
| `python/tests/coding/test_spec35_acceptance_gate.py` | new — G1–G4 + recovery + isolation |

## Risk register

- **Result isolation wrong** → gate reads a probe/unit run as the acceptance verdict.
  Mitigation: G1 cross-references command_id against the live acceptance scope; the
  `::isolation` test is a hard lock.
- **Head resolution wrong** → a stale green allows `done` after a regression, or a
  stale red blocks after a fix. Mitigation: status compares result `head` to the
  *current* master head; `stale` forces a fresh run rather than trusting an old one.
- **Arm loop** → `stale` arms forever if the gate can never produce a result.
  Mitigation: G4 bound + `completion_blocked` escalation; `::G4` test.
- **Chokepoint drift** → the two done paths diverge. Mitigation: both call the one
  `_acceptance_gate_blocks_done` helper; both are tested.
