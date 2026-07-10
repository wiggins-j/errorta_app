#!/usr/bin/env bash
# F-DIST-02 — package a (signed, ideally notarized+stapled) Errorta.app into a
# distributable DMG using hdiutil.
#
# We deliberately do NOT use Tauri's DMG target / create-dmg: it styles the DMG
# window via AppleScript + Finder automation, which fails in non-interactive /
# automation-restricted contexts ("failed to run bundle_dmg.sh" on every build).
# hdiutil needs none of that. The trade-off is a plain (unstyled) DMG window,
# which is fine for the alpha; a branded layout is a v1.0 GA concern.
# See docs/BUILD_AND_RELEASE.md and docs/specs/F-DIST-02-alpha-release-pipeline.md.
#
# Usage:
#   scripts/package-dmg.sh <app-path> <dmg-out> [volname]
#
# Example:
#   scripts/package-dmg.sh \
#     src-tauri/target/release/bundle/macos/Errorta.app \
#     dist/Errorta_0.1.0-alpha.0_aarch64.dmg "Errorta Alpha"
set -euo pipefail

APP="${1:?usage: package-dmg.sh <app-path> <dmg-out> [volname]}"
DMG_OUT="${2:?usage: package-dmg.sh <app-path> <dmg-out> [volname]}"
VOLNAME="${3:-Errorta}"

if [[ ! -d "$APP" ]]; then
  echo "[package-dmg] app not found: $APP" >&2
  exit 1
fi

STAGE="$(mktemp -d)"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

echo "[package-dmg] staging $(basename "$APP") + /Applications symlink"
ditto "$APP" "$STAGE/$(basename "$APP")"
ln -s /Applications "$STAGE/Applications"

mkdir -p "$(dirname "$DMG_OUT")"
rm -f "$DMG_OUT"

echo "[package-dmg] hdiutil create -> $DMG_OUT"
hdiutil create \
  -volname "$VOLNAME" \
  -srcfolder "$STAGE" \
  -ov \
  -format UDZO \
  "$DMG_OUT" >/dev/null

echo "[package-dmg] done: $DMG_OUT ($(du -h "$DMG_OUT" | cut -f1))"
