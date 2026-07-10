#!/usr/bin/env bash
# Publish the Linux AppImage + .deb bundles produced by
# `npm run tauri:build` on a Linux host to the public errorta-downloads
# repo as a GitHub Release (draft by default). Companion to
# scripts/publish-sidecar-release.sh (F-INFRA-13) — see
# docs/specs/F-INFRA-07-linux-appimage-and-deb.md and
# docs/plans/F-INFRA-07.md.
#
# Computes SHA-256 over both artifacts, writes linux-SHA256SUMS.txt
# alongside, attaches all three to the named tag. Auto-detects whether
# the tag already exists on errorta-downloads (a prior F-INFRA-13
# sidecar publish may have created it) — uses `gh release upload
# --clobber` in that case, `gh release create` otherwise.
#
# Per project policy, GitHub Actions stays OFF — this script runs locally
# on the maintainer's hardware (or inside the Linux build VM if `gh` is
# authenticated there).
#
# Usage:
#   bash scripts/publish-linux-release.sh <version-tag>
#   bash scripts/publish-linux-release.sh <version-tag> --no-draft
#
# Example:
#   bash scripts/publish-linux-release.sh v0.4.0-rc1
#   bash scripts/publish-linux-release.sh v0.4.0 --no-draft

set -euo pipefail

REPO="wiggins-j/errorta-downloads"

usage() {
  cat <<EOF
Usage: publish-linux-release.sh <version-tag> [--draft|--no-draft]

  <version-tag>    Required. e.g. v0.4.0-rc1.
  --draft          Default. Creates a draft release on $REPO.
  --no-draft       Release goes live immediately.

Expects Linux build artifacts in src-tauri/target/release/bundle/:
  appimage/Errorta_*_amd64.AppImage
  deb/errorta_*_amd64.deb

Produce them with \`npm run tauri:build\` on a Linux x86_64 host —
see docs/linux-build-vm-setup.md for the runbook.
EOF
}

if [[ $# -lt 1 ]]; then
  usage >&2
  echo "" >&2
  echo "error: missing required <version-tag> argument" >&2
  exit 2
fi

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
esac

TAG="$1"
shift || true

DRAFT_FLAG="--draft"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --draft)
      DRAFT_FLAG="--draft"
      shift
      ;;
    --no-draft)
      DRAFT_FLAG=""
      shift
      ;;
    *)
      echo "error: unknown flag: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

# Validate tag shape (vMAJOR.MINOR.PATCH with optional suffix)
if ! [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+ ]]; then
  echo "error: tag '$TAG' does not look like vMAJOR.MINOR.PATCH" >&2
  exit 2
fi

# Locate repo root (this script's parent's parent).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BUNDLE_DIR="$REPO_ROOT/src-tauri/target/release/bundle"
APPIMAGE_GLOB="$BUNDLE_DIR/appimage/Errorta_*_amd64.AppImage"
DEB_GLOB="$BUNDLE_DIR/deb/errorta_*_amd64.deb"

APPIMAGE="$(ls $APPIMAGE_GLOB 2>/dev/null | head -1 || true)"
DEB="$(ls $DEB_GLOB 2>/dev/null | head -1 || true)"

MISSING=()
[[ -z "$APPIMAGE" ]] && MISSING+=("AppImage not found at $APPIMAGE_GLOB")
[[ -z "$DEB"      ]] && MISSING+=(".deb not found at $DEB_GLOB")

if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "error: missing Linux build artifacts:" >&2
  for m in "${MISSING[@]}"; do
    echo "  - $m" >&2
  done
  echo "" >&2
  echo "Build inside the Linux VM first (docs/linux-build-vm-setup.md):" >&2
  echo "  cd ~/Errorta && bash scripts/build-sidecar.sh && npm run tauri:build" >&2
  exit 1
fi

# Verify gh CLI is on PATH and authenticated.
if ! command -v gh >/dev/null 2>&1; then
  echo "error: gh CLI not found on PATH" >&2
  echo "Install via https://cli.github.com/ then run \`gh auth login\`." >&2
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "error: gh not authenticated" >&2
  echo "Run \`gh auth login\` and grant write access to $REPO." >&2
  exit 1
fi

# Compute SHA-256 over both artifacts; strip the
# src-tauri/target/release/bundle/<dir>/ prefix so the published
# linux-SHA256SUMS.txt contains relative filenames.
SHASUMS_TMP="$(mktemp -t linux-SHA256SUMS.XXXXXX.txt)"
trap 'rm -f "$SHASUMS_TMP"' EXIT

if command -v sha256sum >/dev/null 2>&1; then
  ( cd "$BUNDLE_DIR/appimage" && sha256sum "$(basename "$APPIMAGE")" ) >  "$SHASUMS_TMP"
  ( cd "$BUNDLE_DIR/deb"      && sha256sum "$(basename "$DEB")"      ) >> "$SHASUMS_TMP"
elif command -v shasum >/dev/null 2>&1; then
  ( cd "$BUNDLE_DIR/appimage" && shasum -a 256 "$(basename "$APPIMAGE")" ) >  "$SHASUMS_TMP"
  ( cd "$BUNDLE_DIR/deb"      && shasum -a 256 "$(basename "$DEB")"      ) >> "$SHASUMS_TMP"
else
  echo "error: neither sha256sum nor shasum available on PATH" >&2
  exit 1
fi

echo "[publish-linux-release] tag=$TAG repo=$REPO"
echo "[publish-linux-release] appimage=$APPIMAGE"
echo "[publish-linux-release] deb=$DEB"
echo "[publish-linux-release] sha256sums:"
sed 's/^/  /' "$SHASUMS_TMP"

# Detect existing tag vs. new tag.
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  echo "[publish-linux-release] release $TAG already exists on $REPO — uploading"
  gh release upload "$TAG" \
    --repo "$REPO" \
    --clobber \
    "$APPIMAGE" "$DEB" "$SHASUMS_TMP"
else
  echo "[publish-linux-release] creating release $TAG on $REPO"
  NOTES="Linux artifacts for $TAG. See docs/linux-first-launch.md for install steps."
  if [[ -n "$DRAFT_FLAG" ]]; then
    gh release create "$TAG" \
      --repo "$REPO" \
      --title "Errorta $TAG" \
      "$DRAFT_FLAG" \
      --notes "$NOTES" \
      "$APPIMAGE" "$DEB" "$SHASUMS_TMP"
  else
    gh release create "$TAG" \
      --repo "$REPO" \
      --title "Errorta $TAG" \
      --notes "$NOTES" \
      "$APPIMAGE" "$DEB" "$SHASUMS_TMP"
  fi
fi

URL="$(gh release view "$TAG" --repo "$REPO" --json url -q .url 2>/dev/null || true)"
if [[ -n "$URL" ]]; then
  echo "[publish-linux-release] release URL: $URL"
fi
echo "[publish-linux-release] done"
