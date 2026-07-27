# Running Spec 28 Tier 2 (the live acceptance run)

Tier 1 (`python/tests/coding/test_spec28_autonomy_acceptance.py`) is hermetic,
free, and part of the merge gate. It proves the **harness** can carry a healthy
team to a finished product. It says nothing about whether a real team behaves like
one, because it scripts the model.

Tier 2 is the run that does — weakly, advisorily, and expensively.

## Running it

```bash
bash scripts/live-acceptance.sh
```

That is the only supported entry point. `python/pyproject.toml` sets

```toml
addopts = "-m 'not live and not flaky and not manual'"
```

so `( cd python && pytest )` — the merge gate — cannot reach it. Getting in takes
**both** an explicit marker selection and `ERRORTA_LIVE_ACCEPTANCE=1`; the wrapper
sets both. Anything missing (no configured Council with pm/dev/reviewer members on
live routes, no gateway, no node/Playwright/Chromium) **skips** rather than fails,
so an accidental invocation never starts spending.

## What it costs

Bounds are enforced by the test itself, not by hope:

| Bound | Value | Enforced by |
|---|---|---|
| Model calls | 120 | `reserve_model_calls`, checked before dispatch (concurrent loop included) |
| Iterations | 60 | `max_iterations` |
| Wall clock | 45 min | `should_cancel`, honoured at the top of each iteration and inside the probe's ready-wait |

At 120 frontier calls with coding-sized prompts, expect **single-digit to
low-double-digit dollars** and **20–45 minutes**. The enforced figure is the call
cap, which is exact; the dollar figure is a translation of it — re-derive it from
the run's own usage rollup rather than trusting this table. For scale, the
unbounded 2026-07-24 run was 96 PRs over 3h20m.

## Cadence

**Weekly**, and **mandatory within 7 days of a release cut**. Not nightly: nightly
implies an automation that does not exist here (GitHub Actions is off by a locked
decision), and a schedule nobody runs is worse than an honest weekly one a
maintainer actually performs.

## Reading the result

The wrapper prints the ledger rollup after pytest:

* **`stop_reason`** — the only line that matters. `definition_of_done` is a pass.
  Anything else is a stop, and `errorta status` / the recorded decisions say which
  detector produced it and whether the PM was given a last word first (SPEC-23).
* **`counters`** — iterations, repairs, model escalations, task reassignments. A
  green run that needed escalations is a different animal from one that did not.
* **`prs`** — merged / total. Run 1's calibration point was a 30% merge rate at
  53/96 superseded; anything near that is a red flag even on a `definition_of_done`.
* **`usage`** — the per-member / per-route token rollup. This is where the dollar
  figure above gets re-derived.

`blocking` is deliberately **not** on this test: a live run that fails because a
provider is down or a key expired must not hold a release. The release signal is a
human confirming this passed, not a pytest exit code.
