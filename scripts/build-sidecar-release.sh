#!/usr/bin/env bash
# build-sidecar-release.sh
#
# Provision a fresh release-mode venv (no editable AIAR), build the
# PyInstaller sidecar against it, boot the bundled binary, and assert
# /healthz reports aiar_pin.source: "pinned" and aiar_version: "0.2.0".
#
# Part of F-INFRA-01 Slice (g). See docs/V015_PUBLISH_RUNBOOK.md §11.6.
#
# Distinguishing from scripts/build-sidecar.sh:
#   - build-sidecar.sh uses the dev python/.venv (editable AIAR) and is
#     invoked by `tauri build` via beforeBuildCommand.
#   - build-sidecar-release.sh provisions python/.venv-release fresh with
#     `pip install '.[release]'`, so AIAR comes from PyPI as aiar-rag.
#
# Refuses to run if python/.venv-release already exists — the operator
# must `rm -rf python/.venv-release` first so we never silently reuse a
# stale environment.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_DIR="$REPO_ROOT/python"
BIN_DIR="$REPO_ROOT/src-tauri/binaries"
SPEC="$PY_DIR/sidecar.spec"
RELEASE_VENV="$PY_DIR/.venv-release"

if [[ -e "$RELEASE_VENV" ]]; then
    echo "[build-sidecar-release] $RELEASE_VENV already exists." >&2
    echo "[build-sidecar-release] Delete it first: rm -rf $RELEASE_VENV" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "[build-sidecar-release] python3 not on PATH." >&2
    exit 1
fi

if ! command -v rustc >/dev/null 2>&1; then
    echo "[build-sidecar-release] rustc not on PATH (used for target triple)." >&2
    exit 1
fi

echo "[build-sidecar-release] provisioning fresh venv at $RELEASE_VENV..."
python3 -m venv "$RELEASE_VENV"

VENV_PY="$RELEASE_VENV/bin/python"
VENV_PIP="$RELEASE_VENV/bin/pip"

echo "[build-sidecar-release] upgrading pip + installing Errorta with [release] extras..."
"$VENV_PIP" install --upgrade pip >/dev/null
"$VENV_PIP" install -e "$PY_DIR[release]"

echo "[build-sidecar-release] asserting aiar version inside release venv..."
"$VENV_PY" -c "import aiar; assert aiar.__version__ == '0.2.0', aiar.__version__; print('venv aiar:', aiar.__version__)"

echo "[build-sidecar-release] installing pyinstaller into release venv..."
"$VENV_PIP" install pyinstaller >/dev/null

mkdir -p "$BIN_DIR"

echo "[build-sidecar-release] running pyinstaller..."
(
    cd "$PY_DIR"
    "$RELEASE_VENV/bin/pyinstaller" --noconfirm --clean "$SPEC"
)

TARGET_TRIPLE="$(rustc -Vv | awk '/^host:/ { print $2 }')"
if [[ -z "$TARGET_TRIPLE" ]]; then
    echo "[build-sidecar-release] could not parse target triple from rustc -Vv." >&2
    exit 1
fi

SRC_BIN="$PY_DIR/dist/errorta-sidecar"
EXT=""
if [[ ! -x "$SRC_BIN" ]]; then
    if [[ -x "$SRC_BIN.exe" ]]; then
        SRC_BIN="$SRC_BIN.exe"
        EXT=".exe"
    else
        echo "[build-sidecar-release] expected PyInstaller output at $SRC_BIN" >&2
        exit 1
    fi
fi

DEST_BIN="$BIN_DIR/errorta-sidecar-$TARGET_TRIPLE$EXT"
cp "$SRC_BIN" "$DEST_BIN"
chmod +x "$DEST_BIN"

echo "[build-sidecar-release] staged $DEST_BIN"

# Boot the bundled sidecar on a dedicated port and assert /healthz.
SIDE_PORT="${ERRORTA_RELEASE_PROBE_PORT:-8773}"
echo "[build-sidecar-release] booting bundled sidecar on port $SIDE_PORT..."

ERRORTA_SIDECAR_PORT="$SIDE_PORT" "$DEST_BIN" &
SIDECAR_PID=$!

cleanup() {
    if kill -0 "$SIDECAR_PID" 2>/dev/null; then
        kill "$SIDECAR_PID" 2>/dev/null || true
        wait "$SIDECAR_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Poll /healthz for up to ~30s.
HEALTH_URL="http://127.0.0.1:$SIDE_PORT/healthz"
HEALTH_JSON=""
for _ in $(seq 1 30); do
    sleep 1
    if HEALTH_JSON="$(curl -fsS "$HEALTH_URL" 2>/dev/null)"; then
        break
    fi
done

if [[ -z "$HEALTH_JSON" ]]; then
    echo "[build-sidecar-release] /healthz never came up at $HEALTH_URL" >&2
    exit 1
fi

echo "[build-sidecar-release] /healthz responded; inspecting aiar_pin..."

"$VENV_PY" - <<PY
import json, sys
data = json.loads('''$HEALTH_JSON''')
pin = data.get("aiar_pin") or {}
ver = data.get("aiar_version")
problems = []
if ver != "0.2.0":
    problems.append(f"aiar_version != 0.2.0 (got {ver!r})")
if pin.get("source") != "pinned":
    problems.append(f'aiar_pin.source != "pinned" (got {pin.get("source")!r})')
if pin.get("available") is not True:
    problems.append(f'aiar_pin.available != True (got {pin.get("available")!r})')
if problems:
    print("[build-sidecar-release] /healthz mismatch:", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    print(f"  /healthz payload: {data}", file=sys.stderr)
    sys.exit(1)
print("[build-sidecar-release] /healthz OK: aiar_version=0.2.0, aiar_pin.source=pinned")
PY

echo "[build-sidecar-release] PASSED"
