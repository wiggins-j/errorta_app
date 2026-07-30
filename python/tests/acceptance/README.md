# Acceptance journeys

End-to-end backend user-journey tests that exercise the highest-value human
cases in `docs/TEST_CASES.md`. Each chains a full flow (not a single route) and
is traceable to its directly exercised `TC-NN.M` cases via the module docstring,
test docstring, or inline comments.

These sit **above** the existing per-route/per-component unit and UI tests. They
do not replace the full pytest/vitest matrix; they prove representative
integrated paths a real user walks and leave specialized invariants at their
native layer when that produces a stronger test.

## The Coding Council's end-to-end journey lives elsewhere

Spec 28's autonomy acceptance fixture — one autonomous run driven to
`definition_of_done` on a buildless-web target, with the friction (a rejected
review, a revise, a duplicate task, a context request) that makes the convergence
detectors reachable — is **`python/tests/coding/test_spec28_autonomy_acceptance.py`**,
not this directory. It is sited there deliberately: `tests/coding/conftest.py`
pins `$ERRORTA_HOME`/`$HOME` under `tmp_path` autouse, which is what keeps a run
that builds real git workspaces out of the developer's real `~/.errorta`. The live
(non-gating) tier is `test_spec28_live_smoke.py`, driven by
`scripts/live-acceptance.sh`. Neither claims a `TC-NN.M` id.

## Conventions

- One file per suite: `test_tsNN_<slug>.py`.
- Tag with the plan markers (`docs/TEST_AUTOMATION_PLAN.md`): every file is
  `@pytest.mark.acceptance`, plus `security`/`blocking`/`smoke`/`regression` as
  appropriate.
- Each file lists the `TC-NN.M` cases it covers directly.
- Hermetic: disposable `ERRORTA_HOME` (`tmp_errorta_home`), in-repo fakes only,
  no network.

## Running

```
# all acceptance journeys
pytest tests/acceptance -q
# only the blocking security gate
pytest -m "acceptance and security" -q
```
