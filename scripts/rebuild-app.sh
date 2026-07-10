#!/usr/bin/env bash
# rebuild-app — build the Errorta desktop app from CURRENT code, in one command.
#
# This is the fix half of the build-freshness story: when scripts/app-doctor.sh
# says the installed app is STALE, run this to rebuild the sidecar (stamped with
# the current git commit) + bundle the .app. Pass --install to replace the app
# in /Applications too.
#
#   bash scripts/rebuild-app.sh                 # build only -> prints the .app path
#   bash scripts/rebuild-app.sh --install       # build AND install to /Applications
#
# After installing, relaunch Errorta and run `bash scripts/app-doctor.sh` — it
# should now report CURRENT.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_DIR="$REPO_ROOT/python"
INSTALL=0
APP_DEST="/Applications/Errorta.app"
APP_SRC="$REPO_ROOT/src-tauri/target/release/bundle/macos/Errorta.app"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install) INSTALL=1; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

cd "$REPO_ROOT"
HEAD="$(git rev-parse HEAD)"
echo "[rebuild-app] building from $HEAD ($(git rev-parse --short HEAD))"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "[rebuild-app] WARNING: working tree is dirty — the build will be stamped dirty."
fi

# Guard: a rebuilt sidecar embeds whatever is in python/.venv. If AIAR isn't
# importable there, the new app loses LOCAL corpus grounding (remote AIAR via
# the watchdog still works). Warn loudly rather than silently regress.
if "$PY_DIR/.venv/bin/python" -c "import aiar" >/dev/null 2>&1; then
  echo "[rebuild-app] AIAR present in venv -> local corpus grounding will work."
else
  echo "[rebuild-app] NOTE: AIAR is NOT importable in python/.venv."
  echo "              The rebuilt app will have NO local corpus (remote/watchdog AIAR"
  echo "              still works). To enable local AIAR: clone it and"
  echo "              'python/.venv/bin/pip install -e <aiar-checkout>' then re-run."
fi

# pyinstaller must be available for the sidecar build
if [[ ! -x "$PY_DIR/.venv/bin/pyinstaller" ]] && ! command -v pyinstaller >/dev/null 2>&1; then
  echo "[rebuild-app] ERROR: pyinstaller not found. Run: $PY_DIR/.venv/bin/pip install -e 'python[dev]'" >&2
  exit 1
fi

echo "[rebuild-app] (1/3) building stamped sidecar binary..."
bash "$REPO_ROOT/scripts/build-sidecar.sh"

echo "[rebuild-app] (2/3) bundling the desktop app (npm run tauri:build)..."
npm run tauri:build

if [[ ! -d "$APP_SRC" ]]; then
  echo "[rebuild-app] ERROR: expected bundle not found at $APP_SRC" >&2
  exit 1
fi

# Sanity: the bundled stamp must match HEAD (proves we shipped current code).
STAMP="$PY_DIR/errorta_app/_build_info.json"
if [[ -f "$STAMP" ]]; then
  STAMPED="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("commit",""))' "$STAMP")"
  if [[ "$STAMPED" == "$HEAD" ]]; then
    echo "[rebuild-app] stamp OK: bundled sidecar reports $HEAD"
  else
    echo "[rebuild-app] WARNING: bundled stamp ($STAMPED) != HEAD ($HEAD)"
  fi
fi

echo "[rebuild-app] (3/3) built: $APP_SRC"
if [[ "$INSTALL" == "1" ]]; then
  echo "[rebuild-app] installing to $APP_DEST ..."
  rm -rf "$APP_DEST"
  ditto "$APP_SRC" "$APP_DEST"
  # Bust the WKWebView cache. The frontend is embedded in the binary, but the
  # webview caches the previous build's bundle on disk and will keep serving it
  # after a reinstall — the "I rebuilt but the UI is identical" trap. Clearing
  # it guarantees the new frontend actually loads on next launch.
  rm -rf "$HOME/Library/Caches/com.errorta.app" "$HOME/Library/WebKit/com.errorta.app" 2>/dev/null || true
  echo "[rebuild-app] cleared the webview cache (forces the new UI to load)"
  echo "[rebuild-app] installed. Relaunch Errorta, then: bash scripts/app-doctor.sh  (expect CURRENT)"
else
  echo "[rebuild-app] not installed. To install: drag it to /Applications, or re-run with --install."
fi
