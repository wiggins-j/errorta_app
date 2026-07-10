#!/usr/bin/env bash
# Build the PyInstaller sidecar binary and stage it under
# src-tauri/binaries/ with the platform target-triple suffix Tauri expects
# for an `externalBin` entry.
#
# Invoked by `tauri build` via `beforeBuildCommand` in tauri.conf.json. Safe to
# run by hand during development too:
#
#   bash scripts/build-sidecar.sh                                 # host triple
#   bash scripts/build-sidecar.sh --target <triple>               # cross-build
#   bash scripts/build-sidecar.sh --output-dir /path/to/dir       # override staging
#
# Supported cross-build triples (see docs/data-residency.md → "Cross-arch builds"):
#   x86_64-unknown-linux-gnu     (Docker, native or qemu-x86_64)
#   x86_64-apple-darwin          (Rosetta, requires x86_64 Homebrew + python@3.11)
#   aarch64-unknown-linux-gnu    (Docker buildx + qemu user-mode, ~30 min)
#   x86_64-pc-windows-msvc       (SSH into Windows VM; see Slice (f) runbook)
#
# Requires:
#   - python/.venv with pyinstaller installed (`pip install -e python[dev]`)
#   - rustc (used only to read the host target triple)
#   - For cross-builds: Docker / Rosetta / a Windows VM as listed above.
set -euo pipefail

usage() {
  cat <<EOF
Usage: build-sidecar.sh [--target <triple>] [--output-dir <path>] [--help]

  --target <triple>     Build for the given target triple. If absent, the
                        script detects the host triple via 'rustc -Vv'.
  --output-dir <path>   Override where the staged binary is written.
                        Defaults to <repo>/src-tauri/binaries.
  --help                Print this message and exit.
EOF
}

TARGET_TRIPLE=""
OUTPUT_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      TARGET_TRIPLE="${2:-}"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="${2:-}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "[build-sidecar] unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_DIR="$REPO_ROOT/python"
BIN_DIR="${OUTPUT_DIR:-$REPO_ROOT/src-tauri/binaries}"
SPEC="$PY_DIR/sidecar.spec"

mkdir -p "$BIN_DIR"

# Stamp build provenance so the frozen sidecar can report which commit it was
# built from (/healthz -> build.commit; scripts/app-doctor.sh compares it to the
# repo HEAD to flag a stale bundle). The spec bundles this file when present.
stamp_build_info() {
  local commit dirty built_at alpha_gate=false out="$PY_DIR/errorta_app/_build_info.json"
  commit="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
  dirty=false
  [[ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)" ]] && dirty=true
  built_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  case "${ERRORTA_ALPHA_GATE:-}" in
    1|true|TRUE|yes|YES|on|ON) alpha_gate=true ;;
  esac
  printf '{"commit":"%s","built_at":"%s","dirty":%s,"source":"bundled","alpha_gate_enabled":%s}\n' \
    "$commit" "$built_at" "$dirty" "$alpha_gate" > "$out"
  echo "[build-sidecar] stamped build_info: $commit (dirty=$dirty, alpha_gate=$alpha_gate)"
}
stamp_build_info

# Pick up the venv's pyinstaller if available; otherwise fall back to PATH.
if [[ -x "$PY_DIR/.venv/bin/pyinstaller" ]]; then
  PYINSTALLER="$PY_DIR/.venv/bin/pyinstaller"
elif command -v pyinstaller >/dev/null 2>&1; then
  PYINSTALLER="pyinstaller"
else
  PYINSTALLER=""
fi

# Determine host triple via rustc (used as default + for native-build short-circuit).
HOST_TRIPLE=""
if command -v rustc >/dev/null 2>&1; then
  HOST_TRIPLE="$(rustc -Vv | awk '/^host:/ { print $2 }')"
fi

# If no --target was passed, default to the host triple.
if [[ -z "$TARGET_TRIPLE" ]]; then
  if [[ -z "$HOST_TRIPLE" ]]; then
    echo "[build-sidecar] rustc not found and no --target given; cannot determine triple." >&2
    exit 1
  fi
  TARGET_TRIPLE="$HOST_TRIPLE"
fi

echo "[build-sidecar] target triple: $TARGET_TRIPLE (host: ${HOST_TRIPLE:-unknown})"

# stage_native: run PyInstaller in the host venv and copy the output to BIN_DIR
# with the target-triple suffix. Used for the host-native dispatch branch.
stage_native() {
  local triple="$1"
  if [[ -z "$PYINSTALLER" ]]; then
    echo "[build-sidecar] pyinstaller not found. Activate python/.venv or pip install pyinstaller." >&2
    exit 1
  fi
  echo "[build-sidecar] using $PYINSTALLER"
  (
    cd "$PY_DIR"
    "$PYINSTALLER" --noconfirm --clean "$SPEC"
  )

  local src="$PY_DIR/dist/errorta-sidecar"
  local ext=""
  if [[ ! -x "$src" ]]; then
    if [[ -x "$src.exe" ]]; then
      src="$src.exe"
      ext=".exe"
    else
      echo "[build-sidecar] expected PyInstaller output at $src" >&2
      exit 1
    fi
  fi
  local dest="$BIN_DIR/errorta-sidecar-$triple$ext"
  cp "$src" "$dest"
  chmod +x "$dest"
  echo "[build-sidecar] staged $dest"
}

# Dispatch by target triple. Native (host) builds short-circuit through
# stage_native; cross-builds either spin Docker (Linux), Rosetta (Intel Mac),
# or ssh into a Windows VM.
case "$TARGET_TRIPLE" in
  x86_64-unknown-linux-gnu)
    if [[ "$HOST_TRIPLE" == "x86_64-unknown-linux-gnu" ]]; then
      stage_native "$TARGET_TRIPLE"
    else
      if ! command -v docker >/dev/null 2>&1; then
        echo "[build-sidecar] docker not found; required for x86_64-unknown-linux-gnu cross-build." >&2
        exit 1
      fi
      echo "[build-sidecar] dispatching to Docker (linux/amd64, python:3.11-bookworm)..."
      docker run --rm --platform linux/amd64 \
        -v "$REPO_ROOT":/work \
        python:3.11-bookworm bash -c '
          set -euo pipefail
          cd /work && pip install -e "python[dev]" \
            && cd python && pyinstaller --noconfirm --clean sidecar.spec \
            && cp dist/errorta-sidecar /work/src-tauri/binaries/errorta-sidecar-x86_64-unknown-linux-gnu \
            && chmod +x /work/src-tauri/binaries/errorta-sidecar-x86_64-unknown-linux-gnu
        '
      echo "[build-sidecar] staged $BIN_DIR/errorta-sidecar-x86_64-unknown-linux-gnu"
    fi
    ;;
  x86_64-apple-darwin)
    if [[ "$HOST_TRIPLE" == "x86_64-apple-darwin" ]]; then
      stage_native "$TARGET_TRIPLE"
    else
      X86_PY="${ERRORTA_X86_PYTHON:-/usr/local/opt/python@3.11/bin/python3.11}"
      if [[ ! -x "$X86_PY" ]]; then
        echo "[build-sidecar] x86_64 Python not found at $X86_PY." >&2
        echo "[build-sidecar] Install x86_64 Homebrew + python@3.11, or" >&2
        echo "[build-sidecar] set ERRORTA_X86_PYTHON to a Rosetta-compatible python3.x." >&2
        exit 1
      fi
      # Ensure a Rosetta venv exists with pyinstaller + the sidecar deps.
      ROSETTA_VENV="$PY_DIR/.venv-x86_64"
      if [[ ! -d "$ROSETTA_VENV" ]]; then
        echo "[build-sidecar] creating $ROSETTA_VENV under arch -x86_64..."
        arch -x86_64 "$X86_PY" -m venv "$ROSETTA_VENV"
        arch -x86_64 "$ROSETTA_VENV/bin/pip" install --upgrade pip
        arch -x86_64 "$ROSETTA_VENV/bin/pip" install -e "$PY_DIR[dev]"
      fi
      echo "[build-sidecar] running pyinstaller under arch -x86_64..."
      (
        cd "$PY_DIR"
        arch -x86_64 "$ROSETTA_VENV/bin/pyinstaller" --noconfirm --clean "$SPEC"
      )
      src="$PY_DIR/dist/errorta-sidecar"
      dest="$BIN_DIR/errorta-sidecar-x86_64-apple-darwin"
      cp "$src" "$dest"
      chmod +x "$dest"
      echo "[build-sidecar] staged $dest"
    fi
    ;;
  aarch64-unknown-linux-gnu)
    if [[ "$HOST_TRIPLE" == "aarch64-unknown-linux-gnu" ]]; then
      stage_native "$TARGET_TRIPLE"
    else
      if ! command -v docker >/dev/null 2>&1; then
        echo "[build-sidecar] docker not found; required for aarch64-unknown-linux-gnu cross-build." >&2
        exit 1
      fi
      echo "[build-sidecar] warning: aarch64-linux via qemu emulation takes ~30 min." >&2
      echo "[build-sidecar] dispatching to Docker (linux/arm64, python:3.11-bookworm)..."
      docker run --rm --platform linux/arm64 \
        -v "$REPO_ROOT":/work \
        python:3.11-bookworm bash -c '
          set -euo pipefail
          cd /work && pip install -e "python[dev]" \
            && cd python && pyinstaller --noconfirm --clean sidecar.spec \
            && cp dist/errorta-sidecar /work/src-tauri/binaries/errorta-sidecar-aarch64-unknown-linux-gnu \
            && chmod +x /work/src-tauri/binaries/errorta-sidecar-aarch64-unknown-linux-gnu
        '
      echo "[build-sidecar] staged $BIN_DIR/errorta-sidecar-aarch64-unknown-linux-gnu"
    fi
    ;;
  x86_64-pc-windows-msvc)
    HOST_OS="$(uname -s 2>/dev/null || echo unknown)"
    if [[ "$HOST_OS" == MINGW* || "$HOST_OS" == MSYS* || "$HOST_OS" == CYGWIN* ]]; then
      # Native Windows build (under Git-Bash / MSYS).
      stage_native "$TARGET_TRIPLE"
    else
      VM_HOST="${ERRORTA_WIN_VM_HOST:-windows-vm}"
      VM_PATH="${ERRORTA_WIN_VM_PATH:-/c/errorta}"
      echo "[build-sidecar] dispatching to Windows VM at $VM_HOST:$VM_PATH" >&2
      ssh "$VM_HOST" "cd $VM_PATH && bash scripts/build-sidecar.sh --target x86_64-pc-windows-msvc"
      scp "$VM_HOST:$VM_PATH/src-tauri/binaries/errorta-sidecar-x86_64-pc-windows-msvc.exe" \
          "$BIN_DIR/errorta-sidecar-x86_64-pc-windows-msvc.exe"
      echo "[build-sidecar] staged $BIN_DIR/errorta-sidecar-x86_64-pc-windows-msvc.exe"
    fi
    ;;
  aarch64-apple-darwin)
    # Native on Apple Silicon. Cross from Linux/Windows is not supported
    # (PyInstaller has no remote-arch story for Mach-O); error early if not native.
    if [[ "$HOST_TRIPLE" == "aarch64-apple-darwin" ]]; then
      stage_native "$TARGET_TRIPLE"
    else
      echo "[build-sidecar] aarch64-apple-darwin can only be built on an Apple Silicon Mac." >&2
      exit 1
    fi
    ;;
  *)
    # Unknown triple — try the host-native path; surfaces an error if PyInstaller
    # can't produce a binary for it.
    if [[ "$TARGET_TRIPLE" == "$HOST_TRIPLE" ]]; then
      stage_native "$TARGET_TRIPLE"
    else
      echo "[build-sidecar] no cross-build dispatch for triple: $TARGET_TRIPLE" >&2
      echo "[build-sidecar] supported triples: x86_64-unknown-linux-gnu, x86_64-apple-darwin," >&2
      echo "[build-sidecar]                    aarch64-unknown-linux-gnu, x86_64-pc-windows-msvc," >&2
      echo "[build-sidecar]                    aarch64-apple-darwin (native only)" >&2
      exit 1
    fi
    ;;
esac
