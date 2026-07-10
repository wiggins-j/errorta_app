#!/usr/bin/env bash
# Publish a per-triple matrix of Errorta sidecar binaries to the public
# `errorta-downloads` repo as a draft GitHub Release. Companion to
# scripts/build-sidecar.sh — see F-INFRA-13 + docs/data-residency.md →
# "Cross-arch builds".
#
# Bundles every triple currently staged in src-tauri/binaries/, computes
# SHA256SUMS.txt over the matrix, and runs `gh release create` as a draft
# the maintainer un-drafts manually after spot-checking the asset list.
#
# Per project policy, GitHub Actions stays OFF — this script runs locally on
# the maintainer's hardware. SHA-256 only; GPG signing is out of scope for
# v0.5.x.
#
# Usage:
#   bash scripts/publish-sidecar-release.sh --tag v0.5.0-rc3
#   bash scripts/publish-sidecar-release.sh --tag v0.5.0-rc3 --dry-run
#   bash scripts/publish-sidecar-release.sh --tag v0.5.0-rc3 \
#       --repo wiggins-j/errorta-downloads \
#       --notes-file dist/release-notes.md

set -euo pipefail

usage() {
  cat <<EOF
Usage: publish-sidecar-release.sh --tag <vX.Y.Z> [--repo <slug>] \
                                  [--notes-file <path>] [--dry-run] [--help]

  --tag <vX.Y.Z>       Required. Tag for gh release create.
  --repo <slug>        Default wiggins-j/errorta-downloads.
  --notes-file <path>  Default dist/release-notes.md (relative to CWD).
                       Required unless --dry-run.
  --dry-run            Print the gh release create command, do not execute.
  --help               Print this message and exit.

The script expects the v0.5.x matrix of sidecar binaries to exist under
src-tauri/binaries/ relative to the current working directory:

    errorta-sidecar-aarch64-apple-darwin
    errorta-sidecar-x86_64-apple-darwin
    errorta-sidecar-x86_64-unknown-linux-gnu
    errorta-sidecar-aarch64-unknown-linux-gnu
    errorta-sidecar-x86_64-pc-windows-msvc.exe

Build any missing triples via scripts/build-sidecar.sh --target <triple>
first; the runbook is in docs/data-residency.md → "Cross-arch builds".
EOF
}

TAG=""
REPO="wiggins-j/errorta-downloads"
NOTES="dist/release-notes.md"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)
      TAG="${2:-}"
      shift 2
      ;;
    --repo)
      REPO="${2:-}"
      shift 2
      ;;
    --notes-file)
      NOTES="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "[publish-sidecar-release] unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$TAG" ]]; then
  echo "[publish-sidecar-release] --tag is required" >&2
  usage >&2
  exit 1
fi

# v0.5.x matrix. Edit this array if the maintainer ships a partial release
# (e.g. just the three triples actually used by example-host dogfood).
MATRIX=(
  "errorta-sidecar-aarch64-apple-darwin"
  "errorta-sidecar-x86_64-apple-darwin"
  "errorta-sidecar-x86_64-unknown-linux-gnu"
  "errorta-sidecar-aarch64-unknown-linux-gnu"
  "errorta-sidecar-x86_64-pc-windows-msvc.exe"
)

BIN_DIR="src-tauri/binaries"

# Step 1: gh CLI present + authenticated.
if [[ "$DRY_RUN" -eq 0 ]]; then
  if ! command -v gh >/dev/null 2>&1; then
    echo "[publish-sidecar-release] gh CLI not found in PATH." >&2
    exit 1
  fi
  if ! gh auth status >/dev/null 2>&1; then
    echo "[publish-sidecar-release] gh auth status failed — run 'gh auth login' first." >&2
    exit 1
  fi
fi

# Step 2: every triple in the matrix exists in src-tauri/binaries/.
echo "[publish-sidecar-release] checking matrix in $BIN_DIR..."
MISSING=()
for triple in "${MATRIX[@]}"; do
  if [[ ! -f "$BIN_DIR/$triple" ]]; then
    MISSING+=("$triple")
  fi
done
if [[ "${#MISSING[@]}" -gt 0 ]]; then
  echo "[publish-sidecar-release] missing binaries — build them first:" >&2
  for m in "${MISSING[@]}"; do
    echo "    $BIN_DIR/$m" >&2
  done
  echo "[publish-sidecar-release] see docs/data-residency.md → 'Cross-arch builds'." >&2
  exit 2
fi

# Step 3: compute SHA256SUMS.txt into a tmp dir.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "[publish-sidecar-release] computing SHA256SUMS.txt in $TMP..."
if command -v shasum >/dev/null 2>&1; then
  ( cd "$BIN_DIR" && shasum -a 256 "${MATRIX[@]}" ) > "$TMP/SHA256SUMS.txt"
elif command -v sha256sum >/dev/null 2>&1; then
  ( cd "$BIN_DIR" && sha256sum "${MATRIX[@]}" ) > "$TMP/SHA256SUMS.txt"
else
  echo "[publish-sidecar-release] neither shasum nor sha256sum found." >&2
  exit 1
fi
echo "[publish-sidecar-release] SHA256SUMS.txt:"
sed 's/^/    /' "$TMP/SHA256SUMS.txt"

# Step 4: notes file exists unless dry-run.
if [[ ! -f "$NOTES" ]]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[publish-sidecar-release] --dry-run: synthesizing stub notes at $TMP/release-notes.md"
    printf 'Errorta sidecar binaries — %s\n\nDraft release.\n' "$TAG" > "$TMP/release-notes.md"
    NOTES="$TMP/release-notes.md"
  else
    echo "[publish-sidecar-release] notes file not found: $NOTES" >&2
    echo "[publish-sidecar-release] write the release notes first (or pass --notes-file <path>)." >&2
    exit 1
  fi
fi

# Step 5: build the gh release create command line.
GH_CMD=(gh release create "$TAG"
  --repo "$REPO"
  --notes-file "$NOTES"
  --draft)
for triple in "${MATRIX[@]}"; do
  GH_CMD+=("$BIN_DIR/$triple")
done
GH_CMD+=("$TMP/SHA256SUMS.txt")

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[publish-sidecar-release] dry-run — would execute:"
  printf '    '
  printf '%q ' "${GH_CMD[@]}"
  printf '\n'
  exit 0
fi

echo "[publish-sidecar-release] running gh release create..."
"${GH_CMD[@]}"

# Step 6: print the resulting Release URL + un-draft reminder.
URL="$(gh release view "$TAG" --repo "$REPO" --json url -q .url 2>/dev/null || true)"
if [[ -n "$URL" ]]; then
  echo "[publish-sidecar-release] release: $URL"
fi
echo "[publish-sidecar-release] DRAFT release created. Spot-check the assets in the UI, then un-draft manually."
