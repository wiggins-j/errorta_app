#!/usr/bin/env bash
# verify-aiar-pin-linux.sh
#
# Cross-platform clean-install probe for aiar-rag==0.2.0 on Linux.
# Runs from the maintainer's macOS host via Docker (python:3.11-bookworm).
#
# Part of F-INFRA-01 Slice (f). Idempotent (the --rm flag cleans up the
# container on exit). See docs/V015_PUBLISH_RUNBOOK.md §11.5.
#
# Exit codes:
#   0 — container printed "OK"; aiar-rag==0.2.0 resolved and imported.
#   1 — docker not on PATH (operator needs to install Docker Desktop).
#   non-zero — docker run failed; investigate the printed output.

set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found on PATH." >&2
    echo "Install Docker Desktop (or any docker engine) and re-run." >&2
    exit 1
fi

echo "Running aiar-rag==0.2.0 clean-install probe inside python:3.11-bookworm..."

docker run --rm python:3.11-bookworm bash -c '
    set -e
    python -m venv /tmp/v
    . /tmp/v/bin/activate
    pip install --quiet aiar-rag==0.2.0
    python -c "import aiar; assert aiar.__version__ == \"0.2.0\", aiar.__version__; print(\"OK\")"
'

echo "Linux clean-install probe: PASSED"
