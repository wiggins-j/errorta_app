#!/usr/bin/env bash
# Spec 28 (Item 7) — Tier 2: the LIVE acceptance smoke run.
#
# Runs the Spec 28 fixture's north star against REAL members on real routes with a
# real browser. It spends real money and real time. It is NEVER part of the merge
# gate: `python/pyproject.toml` sets `addopts = -m 'not live and not flaky and not
# manual'`, so `( cd python && pytest )` cannot reach it — this wrapper is the only
# supported way in.
#
# Cadence: weekly, and mandatory within 7 days of a release cut. The result is a
# human sign-off, not a pytest exit code — a live run that fails because a provider
# is down or a key expired must not hold a release.
#
# Bounds enforced by the test itself (see test_spec28_live_smoke.py):
#   max_model_calls = 120  (hard, checked before dispatch)
#   max_iterations  = 60
#   wall clock      = 45 minutes (should_cancel)
#
# Usage:
#   bash scripts/live-acceptance.sh [--help]
#
# Prerequisites: a configured Council with pm/dev/reviewer members on live gateway
# routes, and node + Playwright + a Chromium binary. Anything missing SKIPS rather
# than fails, so an accidental invocation never starts spending.
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  sed -n '2,24p' "$0"
  exit 0
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/python"

PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  if [[ -x ".venv/bin/python" ]]; then PY=".venv/bin/python"; else PY="python3"; fi
fi

echo "== Spec 28 Tier 2 — live acceptance run =="
echo "   python:  $PY"
echo "   guard:   ERRORTA_LIVE_ACCEPTANCE=1"
echo "   bounds:  120 model calls / 60 iterations / 45 min"
echo

set +e
ERRORTA_LIVE_ACCEPTANCE=1 "$PY" -m pytest -m live \
  tests/coding/test_spec28_live_smoke.py -q -rs
STATUS=$?
set -e

echo
echo "== ledger rollup =="
# The stop reason and the model-usage rollup are the two things a maintainer signs
# off on; read them from the run the test just created (newest spec28-live-* id).
ERRORTA_LIVE_ACCEPTANCE=1 "$PY" - <<'PY' || true
from errorta_council.coding.ledger import LedgerStore, list_projects
from errorta_council.coding.usage_rollup import rollup_turns

try:
    ids = [str(p.get("id") or "") for p in list_projects()
           if str(p.get("id") or "").startswith("spec28-live-")]
except Exception as exc:  # noqa: BLE001
    print(f"(could not enumerate projects: {exc})")
    ids = []
if not ids:
    print("(no spec28-live-* project found — the run skipped)")
else:
    store = LedgerStore(sorted(ids)[-1])
    state = store.get_run_state()
    prs = store.list_prs()
    merged = sum(1 for p in prs if p.get("status") == "merged")
    print(f"project:     {store.project_id}")
    print(f"stop_reason: {state.get('stop_reason')}")
    print(f"counters:    {state.get('counters')}")
    print(f"prs:         {merged}/{len(prs)} merged")
    try:
        print(f"usage:       {rollup_turns(store.list_turns())}")
    except Exception as exc:  # noqa: BLE001
        print(f"usage:       (unavailable: {exc})")
PY

echo
echo "Sign-off is a human decision. pytest exit status: $STATUS"
exit "$STATUS"
