# Acceptance journeys

End-to-end backend user-journey tests that exercise the highest-value human
cases in `docs/TEST_CASES.md`. Each chains a full flow (not a single route) and
is traceable to its directly exercised `TC-NN.M` cases via the module docstring,
test docstring, or inline comments.

These sit **above** the existing per-route/per-component unit and UI tests. They
do not replace the full pytest/vitest matrix; they prove representative
integrated paths a real user walks and leave specialized invariants at their
native layer when that produces a stronger test.

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
