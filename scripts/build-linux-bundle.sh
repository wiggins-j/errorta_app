#!/usr/bin/env bash
# Drive a Linux AppImage + .deb build on a remote x86_64 Linux host
# from the maintainer's macOS laptop. rsync's the source tree to the
# remote, runs the sidecar build + `npm run tauri:build` over SSH, and
# scps the produced artifacts back to the local working tree.
#
# This is the pragmatic alternative to running a Parallels Ubuntu VM
# locally — the maintainer's home Linux box (default: ssh alias
# `example-host`) plays the same role. See docs/linux-build-vm-setup.md
# for the canonical VM runbook; this script automates the "build on a
# friendly Linux box" path once that box has Tauri 2's prerequisites
# installed.
#
# Per project policy, no GitHub Actions. Build runs on the maintainer's
# hardware (the remote Linux box still counts as the maintainer's
# hardware).
#
# Usage:
#   bash scripts/build-linux-bundle.sh [--host <ssh-alias>] [--remote-dir <path>]
#
# Example:
#   bash scripts/build-linux-bundle.sh                                # example-host:~/Errorta-build
#   bash scripts/build-linux-bundle.sh --host my-linux-box
#
# Prerequisites on the remote host (one-time):
#   sudo apt update && sudo apt install -y \
#     libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev \
#     librsvg2-dev libssl-dev pkg-config build-essential \
#     curl wget file python3-pip python3-venv patchelf
#   curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
#   sudo apt install -y nodejs
#   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
#
# The script does NOT install anything on the remote host. Missing
# tooling is reported as a precondition error.

set -euo pipefail

REMOTE_HOST="example-host"
REMOTE_DIR="~/Errorta-build"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      REMOTE_HOST="$2"
      shift 2
      ;;
    --remote-dir)
      REMOTE_DIR="$2"
      shift 2
      ;;
    -h|--help)
      sed -n '1,40p' "$0"
      exit 0
      ;;
    *)
      echo "error: unknown flag: $1" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[build-linux-bundle] host=$REMOTE_HOST remote-dir=$REMOTE_DIR"
echo "[build-linux-bundle] repo-root=$REPO_ROOT"

# Step 0 — probe SSH + required tooling on the remote host. Refuse to
# proceed if any prerequisite is missing.
echo "[build-linux-bundle] probing remote tooling..."
PROBE_OUTPUT="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE_HOST" '
  set -e
  command -v node    >/dev/null 2>&1 || { echo "MISSING node";    exit 0; }
  command -v cargo   >/dev/null 2>&1 || { echo "MISSING cargo";   exit 0; }
  command -v rustc   >/dev/null 2>&1 || { echo "MISSING rustc";   exit 0; }
  command -v rsync   >/dev/null 2>&1 || { echo "MISSING rsync";   exit 0; }
  command -v patchelf >/dev/null 2>&1 || { echo "MISSING patchelf"; exit 0; }
  pkg-config --exists webkit2gtk-4.1  || { echo "MISSING libwebkit2gtk-4.1-dev"; exit 0; }
  pkg-config --exists gtk+-3.0        || { echo "MISSING libgtk-3-dev"; exit 0; }
  pkg-config --exists ayatana-appindicator3-0.1 || { echo "MISSING libayatana-appindicator3-dev"; exit 0; }
  pkg-config --exists librsvg-2.0     || { echo "MISSING librsvg2-dev"; exit 0; }
  echo "OK"
' 2>&1)" || true

if [[ "$PROBE_OUTPUT" != "OK" ]]; then
  echo "error: remote host $REMOTE_HOST is missing required tooling:" >&2
  echo "  $PROBE_OUTPUT" >&2
  echo "" >&2
  echo "Install the listed packages on the remote host before retrying." >&2
  echo "See the prerequisites in the script header or" >&2
  echo "docs/linux-build-vm-setup.md for the canonical apt line." >&2
  exit 1
fi

echo "[build-linux-bundle] remote tooling OK"

# Step 1 — rsync the source tree. Exclude bulky / generated trees.
echo "[build-linux-bundle] syncing source to $REMOTE_HOST:$REMOTE_DIR..."
ssh "$REMOTE_HOST" "mkdir -p $REMOTE_DIR"
rsync -az --delete \
  --exclude '.git/' \
  --exclude 'node_modules/' \
  --exclude 'dist/' \
  --exclude 'src-tauri/target/' \
  --exclude 'src-tauri/binaries/' \
  --exclude 'python/.venv/' \
  --exclude 'python/dist/' \
  --exclude 'python/build/' \
  --exclude '.claude/' \
  "$REPO_ROOT/" "$REMOTE_HOST:$REMOTE_DIR/"

# Step 2 — build sidecar + tauri bundle on the remote.
echo "[build-linux-bundle] running build on $REMOTE_HOST..."
ssh "$REMOTE_HOST" "bash -lc '
  set -e
  cd $REMOTE_DIR
  if [[ ! -d python/.venv ]]; then
    python3 -m venv python/.venv
  fi
  source python/.venv/bin/activate
  pip install --quiet -e ./python[dev]
  bash scripts/build-sidecar.sh
  npm install
  npm run tauri:build
'"

# Step 3 — scp produced artifacts back into the local working tree
# under src-tauri/target/release/bundle/ so publish-linux-release.sh
# can find them.
echo "[build-linux-bundle] copying artifacts back..."
mkdir -p "$REPO_ROOT/src-tauri/target/release/bundle/appimage"
mkdir -p "$REPO_ROOT/src-tauri/target/release/bundle/deb"

scp "$REMOTE_HOST:$REMOTE_DIR/src-tauri/target/release/bundle/appimage/Errorta_*_amd64.AppImage" \
  "$REPO_ROOT/src-tauri/target/release/bundle/appimage/" || \
  { echo "error: no AppImage produced on remote" >&2; exit 1; }

scp "$REMOTE_HOST:$REMOTE_DIR/src-tauri/target/release/bundle/deb/errorta_*_amd64.deb" \
  "$REPO_ROOT/src-tauri/target/release/bundle/deb/" || \
  { echo "error: no .deb produced on remote" >&2; exit 1; }

echo "[build-linux-bundle] artifacts in:"
ls -la "$REPO_ROOT/src-tauri/target/release/bundle/appimage/" 2>&1 | tail -5
ls -la "$REPO_ROOT/src-tauri/target/release/bundle/deb/" 2>&1 | tail -5

echo "[build-linux-bundle] done. Next: bash scripts/publish-linux-release.sh <tag>"
