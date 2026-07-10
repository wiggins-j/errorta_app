#!/usr/bin/env bash
# Re-runnable verifier for the F-INFRA-00 v0.1.5 gating checks.
#
# Encodes the four pre-tag checks proven by the manual walk-through in
# docs/specs/F-INFRA-00-aiar-license-and-pypi-name.md slice (a):
#
#   1. LICENSE + NOTICE ship inside the installed aiar-rag distribution.
#   2. The aiar-rag metadata license field carries Apache-2.0.
#   3. `import aiar; aiar.__version__` resolves and starts with 0.2.
#   4. Sidecar /healthz reports aiar_pin.source == "pinned" and the
#      reported version matches aiar.__version__.
#
# This script is NOT a CI workflow. Per locked decision, GitHub Actions
# stays OFF; the maintainer runs this locally before cutting a release
# tag (v0.1.5 / v0.3.0 BENCHMARK / v0.5.0).
#
# Usage:
#   bash scripts/verify-aiar-pin.sh
#
# Exit code: 0 on full pass; 1 on any check failure or environment gap.
# Each failure prints a pointer to the spec.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPEC_REF="docs/specs/F-INFRA-00-aiar-license-and-pypi-name.md"

# --- resolve Python interpreter ----------------------------------------------
PY=""
if [ -x "$REPO_ROOT/python/.venv/bin/python" ]; then
  PY="$REPO_ROOT/python/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "verify-aiar-pin.sh: no python interpreter found." >&2
  echo "  expected $REPO_ROOT/python/.venv/bin/python or python3 on PATH." >&2
  echo "  see DEVELOPING.md for sidecar venv setup." >&2
  exit 1
fi

# --- result tracking ---------------------------------------------------------
exit_code=0
check1_status="PENDING"
check2_status="PENDING"
check3_status="PENDING"
check4_status="PENDING"
aiar_version=""

fail_pointer() {
  echo "See $SPEC_REF"
}

# --- check 1: LICENSE + NOTICE present ---------------------------------------
echo -n "[check 1/4] LICENSE + NOTICE in installed aiar-rag ... "
if out="$("$PY" - <<'PY' 2>&1
import importlib.metadata as m
import sys
try:
    files = [str(p) for p in (m.distribution('aiar-rag').files or [])]
except m.PackageNotFoundError:
    print('aiar-rag not installed')
    sys.exit(2)
has_license = any(f.endswith('LICENSE') for f in files)
has_notice = any(f.endswith('NOTICE') for f in files)
if not has_license:
    print('LICENSE missing from installed aiar-rag')
    sys.exit(3)
if not has_notice:
    print('NOTICE missing from installed aiar-rag')
    sys.exit(4)
print('ok')
PY
)"; then
  echo "OK"
  check1_status="OK"
else
  echo "FAIL: $out"
  check1_status="FAIL"
  exit_code=1
  fail_pointer
fi

# --- check 2: license field is Apache-2.0 ------------------------------------
echo -n "[check 2/4] aiar-rag metadata License field contains Apache-2.0 ... "
if out="$("$PY" - <<'PY' 2>&1
import importlib.metadata as m
import sys
try:
    md = m.metadata('aiar-rag')
except m.PackageNotFoundError:
    print('aiar-rag not installed')
    sys.exit(2)
val = md.get('License-Expression') or md.get('License') or ''
if 'Apache-2.0' not in val:
    print(f'License field does not contain Apache-2.0: {val!r}')
    sys.exit(3)
print('ok')
PY
)"; then
  echo "OK"
  check2_status="OK"
else
  echo "FAIL: $out"
  check2_status="FAIL"
  exit_code=1
  fail_pointer
fi

# --- check 3: import aiar; __version__ starts with 0.2. ----------------------
echo -n "[check 3/4] import aiar; aiar.__version__ starts with 0.2. ... "
if out="$("$PY" - <<'PY' 2>&1
import sys
try:
    import aiar
except ImportError as exc:
    print(f'import aiar failed: {exc}')
    sys.exit(2)
v = getattr(aiar, '__version__', '')
if not isinstance(v, str) or not v.startswith('0.2.'):
    print(f'unexpected aiar version: {v!r}')
    sys.exit(3)
print(v)
PY
)"; then
  aiar_version="$out"
  echo "OK (aiar $aiar_version)"
  check3_status="OK"
else
  echo "FAIL: $out"
  check3_status="FAIL"
  exit_code=1
  fail_pointer
fi

# --- check 4: sidecar /healthz reports aiar_pin.source == "pinned" ----------
SIDECAR_PORT="${ERRORTA_SIDECAR_PORT:-8779}"
SIDECAR_PID=""
sidecar_cleanup() {
  if [ -n "$SIDECAR_PID" ] && kill -0 "$SIDECAR_PID" 2>/dev/null; then
    kill "$SIDECAR_PID" 2>/dev/null || true
    wait "$SIDECAR_PID" 2>/dev/null || true
  fi
}
trap sidecar_cleanup EXIT

echo -n "[check 4/4] sidecar /healthz reports aiar_pin.source == \"pinned\" ... "

# Boot sidecar in background. Run from the python/ subdir so the package
# is importable without further PYTHONPATH manipulation.
(
  cd "$REPO_ROOT/python" && \
  ERRORTA_SIDECAR_PORT="$SIDECAR_PORT" "$PY" -m errorta_app.server \
    > /tmp/verify-aiar-pin-sidecar.log 2>&1
) &
SIDECAR_PID=$!

# Poll /healthz for up to 10 seconds.
healthz=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if healthz="$(curl -fsS "http://127.0.0.1:$SIDECAR_PORT/healthz" 2>/dev/null)"; then
    break
  fi
  sleep 1
done

if [ -z "$healthz" ]; then
  echo "FAIL: sidecar did not respond on port $SIDECAR_PORT within 10s"
  echo "  see /tmp/verify-aiar-pin-sidecar.log for sidecar output"
  check4_status="FAIL"
  exit_code=1
  fail_pointer
else
  if out="$(printf '%s' "$healthz" | EXPECTED_AIAR_VERSION="$aiar_version" "$PY" -c "
import json, os, sys
data = json.load(sys.stdin)
pin = data.get('aiar_pin') or {}
src = pin.get('source')
ver = pin.get('version')
expected_ver = os.environ.get('EXPECTED_AIAR_VERSION', '')
if src != 'pinned':
    print(f'expected aiar_pin.source == \"pinned\", got {pin!r}')
    sys.exit(2)
if expected_ver and ver != expected_ver:
    print(f'aiar_pin.version ({ver!r}) != imported aiar.__version__ ({expected_ver!r})')
    sys.exit(3)
print(f'source=pinned version={ver}')
" 2>&1)"; then
    echo "OK ($out)"
    check4_status="OK"
  else
    rc=$?
    echo "FAIL: $out"
    check4_status="FAIL"
    exit_code=1
    fail_pointer
  fi
fi

sidecar_cleanup
SIDECAR_PID=""

# --- summary -----------------------------------------------------------------
echo ""
echo "F-INFRA-00 verifier summary"
echo "  repo root:         $REPO_ROOT"
echo "  python:            $PY"
echo "  check 1 (license): $check1_status"
echo "  check 2 (apache):  $check2_status"
echo "  check 3 (import):  $check3_status"
echo "  check 4 (healthz): $check4_status"
echo "  exit code:         $exit_code"

exit "$exit_code"
