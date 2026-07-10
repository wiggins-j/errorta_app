#!/usr/bin/env bash
# F-INFRA-02 + F-DIST-02 — local Apple release pipeline (one command).
#
# Produces a Developer-ID-signed, notarized, stapled Errorta .dmg entirely on
# the maintainer's Mac. No CI, no GitHub Actions.
#
# Usage:
#   bash scripts/release-macos.sh <tag> [--alpha]
#
#   <tag>     release label (e.g. v0.6.0-alpha.1); recorded in the summary.
#   --alpha   build with the F-DIST-01 alpha GATE ON (invite-code activation
#             required at first run). Omit for a production, gate-OFF build.
#
# Credentials:
#   APPLE_SIGNING_IDENTITY  — from ~/.config/errorta-release.env or the env.
#   notarization            — the `errorta-notary` keychain profile (preferred),
#                             else APPLE_ID / APPLE_TEAM_ID / APPLE_APP_SPECIFIC_PASSWORD.
#
# Full runbook + manual fallback: docs/BUILD_AND_RELEASE.md
# Signing/cert/keychain deep reference: docs/SIGNING_MACOS.md
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
# shellcheck source=scripts/lib/notarize.sh
source "$REPO_ROOT/scripts/lib/notarize.sh"
# shellcheck source=scripts/lib/verify-release.sh
source "$REPO_ROOT/scripts/lib/verify-release.sh"

# --- args ---
TAG=""
ALPHA_GATE=0
for arg in "$@"; do
  case "$arg" in
    --alpha) ALPHA_GATE=1 ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    --*) echo "[release-macos] unknown flag: $arg" >&2; exit 2 ;;
    *)   if [[ -z "$TAG" ]]; then TAG="$arg"; else echo "[release-macos] unexpected arg: $arg" >&2; exit 2; fi ;;
  esac
done
: "${TAG:?usage: release-macos.sh <tag> [--alpha]}"

# --- clean-HEAD guard: never build a release from a dirty/unexpected tree ---
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
  echo "[release-macos] working tree is dirty — commit or stash before releasing:" >&2
  git -C "$REPO_ROOT" status --short >&2
  exit 1
fi
BUILD_COMMIT="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
GATE_LABEL="$([[ $ALPHA_GATE -eq 1 ]] && echo ON || echo off)"
echo "[release-macos] tag=$TAG  gate=$GATE_LABEL  commit=$BUILD_COMMIT"

# --- signing identity ---
ENV_FILE="${HOME}/.config/errorta-release.env"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi
: "${APPLE_SIGNING_IDENTITY:?APPLE_SIGNING_IDENTITY must be set (see docs/SIGNING_MACOS.md)}"
export APPLE_SIGNING_IDENTITY
export ERRORTA_CODESIGN_IDENTITY="${ERRORTA_CODESIGN_IDENTITY:-$APPLE_SIGNING_IDENTITY}"
export ERRORTA_ENTITLEMENTS_PLIST="$REPO_ROOT/src-tauri/macos/entitlements.plist"
if ! security find-identity -v -p codesigning | grep -F "$APPLE_SIGNING_IDENTITY" >/dev/null; then
  echo "[release-macos] signing identity not in codesigning keychain: $APPLE_SIGNING_IDENTITY" >&2
  echo "[release-macos] see docs/SIGNING_MACOS.md" >&2
  exit 1
fi

# --- fail fast if notarization creds are absent (before the long build) ---
if [[ -z "$(_notary_creds_mode)" ]]; then
  echo "[release-macos] no notarization credentials — set up '$ERRORTA_NOTARY_PROFILE' (xcrun notarytool store-credentials) or env vars first (docs/SIGNING_MACOS.md)." >&2
  exit 1
fi

# --- gate is an explicit build parameter (baked into _build_info.json by
#     build-sidecar.sh via beforeBuildCommand; exported here so it covers the
#     whole build, not just a standalone sidecar step) ---
if [[ $ALPHA_GATE -eq 1 ]]; then
  export ERRORTA_ALPHA_GATE=1
else
  unset ERRORTA_ALPHA_GATE 2>/dev/null || true
fi

APP="$REPO_ROOT/src-tauri/target/release/bundle/macos/Errorta.app"
VERSION="$(python3 -c "import json;print(json.load(open('src-tauri/tauri.conf.json'))['version'])")"
DMG="$REPO_ROOT/dist/Errorta_${VERSION}_aarch64.dmg"

# --- build (tauri.conf targets=["app"] -> no create-dmg; tauri:build exits 0) ---
echo "[release-macos] building (npm run tauri:build) ..."
npm run tauri:build
[[ -d "$APP" ]] || { echo "[release-macos] no .app produced at $APP" >&2; exit 1; }

# --- verify the signature Tauri applied ---
codesign --verify --deep --strict "$APP"

# --- notarize + staple the .app ---
echo "[release-macos] notarizing the app ..."
notarize_app "$APP"

# --- package the DMG via hdiutil (no create-dmg) ---
VOLNAME="Errorta"; [[ $ALPHA_GATE -eq 1 ]] && VOLNAME="Errorta Alpha"
bash "$REPO_ROOT/scripts/package-dmg.sh" "$APP" "$DMG" "$VOLNAME"

# --- notarize + staple the DMG ---
echo "[release-macos] notarizing the dmg ..."
notarize_and_staple "$DMG"

# --- self-verify (fails the release if anything is wrong) ---
verify_release "$APP" "$DMG" "$ALPHA_GATE"

# --- summary ---
SHA256="$(shasum -a 256 "$DMG" | awk '{print $1}')"
echo ""
echo "[release-macos] DONE"
echo "  tag:     $TAG"
echo "  commit:  $BUILD_COMMIT"
echo "  gate:    $GATE_LABEL"
echo "  dmg:     $DMG"
echo "  sha256:  $SHA256"
echo "  next:    host on the unlisted errorta-downloads page, then mint + send codes (docs/ALPHA_LAUNCH.md Part 4)"
