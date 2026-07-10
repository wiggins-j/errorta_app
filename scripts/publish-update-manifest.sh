#!/usr/bin/env bash
# F-INFRA-09 Slice 3 — Sign + publish a Tauri-v2 updater manifest for a
# single (platform, arch) artifact to the public `errorta-downloads` repo.
#
# Per project policy, GitHub Actions stays OFF. This script runs locally on
# the maintainer's offline laptop, holds the updater private key in plain
# sight only for the duration of the run, and pushes both the manifest
# (into errorta-downloads/updates/) and the .sig companion (onto the
# GitHub Release page) in a single invocation.
#
# Companion to:
#   - scripts/build-sidecar.sh           (F-INFRA-13 cross-arch builds)
#   - scripts/publish-sidecar-release.sh (F-INFRA-13 release publish)
# This script handles ONLY the auto-updater manifest piece. Build + bundle
# steps live in the Tauri bundler invocation upstream of this.
#
# Usage:
#   scripts/publish-update-manifest.sh <vX.Y.Z>                  \
#       --platform   <darwin-aarch64|darwin-x86_64|              \
#                     linux-x86_64|linux-aarch64|windows-x86_64> \
#       --artifact-url   <https://.../release/asset.tar.gz>      \
#       --artifact-local <path/to/local/artifact.tar.gz>         \
#       --notes-file     <path/to/release-notes.md>              \
#       --key-path       <path/to/private/updater.key>           \
#       [--channel stable|beta]                                  \
#       [--draft|--no-draft]                                     \
#       [--repo wiggins-j/errorta-downloads]                     \
#       [--dry-run]

set -euo pipefail

usage() {
  cat <<EOF
Usage: publish-update-manifest.sh <version-tag> \\
    --platform   <triple>          \\
    --artifact-url   <url>         \\
    --artifact-local <path>        \\
    --notes-file     <path>        \\
    --key-path       <path>        \\
   [--channel stable|beta]         \\
   [--draft|--no-draft]            \\
   [--repo wiggins-j/errorta-downloads] \\
   [--dry-run]

Required:
  <version-tag>           e.g. v0.6.0 — used as the GitHub Release tag.
  --platform <triple>     one of:
                            darwin-aarch64  darwin-x86_64
                            linux-x86_64    linux-aarch64
                            windows-x86_64
  --artifact-url   <url>  https URL of the release asset on errorta-downloads.
  --artifact-local <path> local path of the same artifact (signed in place).
  --notes-file     <path> release notes markdown, dropped into manifest.notes.
  --key-path       <path> updater private key (Tauri-v2 ed25519 format).
                          See docs/AUTO_UPDATER.md → "Private-key custody".

Optional:
  --channel  stable|beta  default stable. Beta writes to updates/beta/...
  --draft / --no-draft    default --draft. Push to errorta-downloads but skip
                          the final un-draft step; maintainer reviews first.
  --repo <slug>           default wiggins-j/errorta-downloads.
  --dry-run               print the planned actions, do not sign / push.
  --help                  this message.

Environment:
  ERRORTA_UPDATER_KEY_PASSWORD  passphrase for the private key (required
                                unless --dry-run; if unset, the script
                                prompts on tty).

Locked decisions enforced by this script:
  - https only: --artifact-url must start with https://
  - No GitHub Actions: gh CLI runs from the local shell only.
EOF
}

# ---------------------------------------------------------------------------
# Arg parse
# ---------------------------------------------------------------------------
if [[ $# -lt 1 ]] || [[ "${1:-}" == "--help" ]] || [[ "${1:-}" == "-h" ]]; then
  usage
  if [[ "${1:-}" == "--help" ]] || [[ "${1:-}" == "-h" ]]; then exit 0; fi
  exit 2
fi

TAG="$1"
shift

PLATFORM=""
ARTIFACT_URL=""
ARTIFACT_LOCAL=""
NOTES_FILE=""
KEY_PATH=""
CHANNEL="stable"
DRAFT=1
DRAFT_SET=0
REPO="wiggins-j/errorta-downloads"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform)        PLATFORM="${2:-}"; shift 2 ;;
    --artifact-url)    ARTIFACT_URL="${2:-}"; shift 2 ;;
    --artifact-local)  ARTIFACT_LOCAL="${2:-}"; shift 2 ;;
    --notes-file)      NOTES_FILE="${2:-}"; shift 2 ;;
    --key-path)        KEY_PATH="${2:-}"; shift 2 ;;
    --channel)         CHANNEL="${2:-}"; shift 2 ;;
    --draft)
      if [[ $DRAFT_SET -eq 1 ]] && [[ $DRAFT -eq 0 ]]; then
        echo "error: --draft and --no-draft are mutually exclusive" >&2
        exit 2
      fi
      DRAFT=1; DRAFT_SET=1; shift ;;
    --no-draft)
      if [[ $DRAFT_SET -eq 1 ]] && [[ $DRAFT -eq 1 ]]; then
        echo "error: --draft and --no-draft are mutually exclusive" >&2
        exit 2
      fi
      DRAFT=0; DRAFT_SET=1; shift ;;
    --repo)            REPO="${2:-}"; shift 2 ;;
    --dry-run)         DRY_RUN=1; shift ;;
    --help|-h)         usage; exit 0 ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
if [[ -z "$PLATFORM" ]]; then
  echo "error: --platform is required" >&2; exit 2
fi
case "$PLATFORM" in
  darwin-aarch64|darwin-x86_64|linux-x86_64|linux-aarch64|windows-x86_64) ;;
  *)
    echo "error: --platform must be one of darwin-aarch64, darwin-x86_64, linux-x86_64, linux-aarch64, windows-x86_64 (got: $PLATFORM)" >&2
    exit 2
    ;;
esac

case "$CHANNEL" in
  stable|beta) ;;
  *)
    echo "error: --channel must be 'stable' or 'beta' (got: $CHANNEL)" >&2
    exit 2
    ;;
esac

if [[ -z "$ARTIFACT_URL" ]]; then
  echo "error: --artifact-url is required" >&2; exit 2
fi
case "$ARTIFACT_URL" in
  https://*) ;;
  *)
    echo "error: --artifact-url must be https:// (got: $ARTIFACT_URL)" >&2
    exit 2
    ;;
esac

if [[ -z "$ARTIFACT_LOCAL" ]]; then
  echo "error: --artifact-local is required" >&2; exit 2
fi
if [[ ! -f "$ARTIFACT_LOCAL" ]]; then
  echo "error: --artifact-local path does not exist: $ARTIFACT_LOCAL" >&2
  exit 2
fi

if [[ -z "$NOTES_FILE" ]]; then
  echo "error: --notes-file is required" >&2; exit 2
fi
if [[ ! -f "$NOTES_FILE" ]]; then
  echo "error: --notes-file path does not exist: $NOTES_FILE" >&2
  exit 2
fi

if [[ -z "$KEY_PATH" ]]; then
  echo "error: --key-path is required" >&2; exit 2
fi
if [[ $DRY_RUN -eq 0 ]] && [[ ! -f "$KEY_PATH" ]]; then
  echo "error: --key-path does not exist: $KEY_PATH" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# Tool checks
# ---------------------------------------------------------------------------
if [[ $DRY_RUN -eq 0 ]]; then
  if ! command -v gh >/dev/null 2>&1; then
    echo "error: gh CLI not on PATH. Install from https://cli.github.com/." >&2
    exit 1
  fi
  if ! gh auth status >/dev/null 2>&1; then
    echo "error: gh is not authenticated. Run 'gh auth login' first." >&2
    exit 1
  fi
  if ! command -v npx >/dev/null 2>&1; then
    echo "error: npx not on PATH. Install Node.js." >&2
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
case "$CHANNEL" in
  stable) MANIFEST_PATH="updates/${PLATFORM}.json" ;;
  beta)   MANIFEST_PATH="updates/beta/${PLATFORM}.json" ;;
esac

SIG_PATH="${ARTIFACT_LOCAL}.sig"
PUB_DATE="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

echo "publish-update-manifest.sh plan:"
echo "  tag           = $TAG"
echo "  platform      = $PLATFORM"
echo "  channel       = $CHANNEL"
echo "  artifact-url  = $ARTIFACT_URL"
echo "  artifact-local= $ARTIFACT_LOCAL"
echo "  notes-file    = $NOTES_FILE"
echo "  key-path      = $KEY_PATH"
echo "  repo          = $REPO"
echo "  draft         = $DRAFT"
echo "  manifest path = $MANIFEST_PATH"
echo "  pub_date      = $PUB_DATE"

if [[ $DRY_RUN -eq 1 ]]; then
  echo "[dry-run] no signing / push will happen"
  exit 0
fi

# ---------------------------------------------------------------------------
# Sign
# ---------------------------------------------------------------------------
PASSWORD="${ERRORTA_UPDATER_KEY_PASSWORD:-}"
if [[ -z "$PASSWORD" ]]; then
  read -r -s -p "Enter updater key passphrase: " PASSWORD
  echo
fi

echo "[1/4] signing $ARTIFACT_LOCAL ..."
TAURI_PRIVATE_KEY="$(cat "$KEY_PATH")" \
TAURI_KEY_PASSWORD="$PASSWORD" \
  npx @tauri-apps/cli signer sign --private-key "$KEY_PATH" \
    --password "$PASSWORD" \
    "$ARTIFACT_LOCAL"

if [[ ! -f "$SIG_PATH" ]]; then
  echo "error: expected signature companion not produced at $SIG_PATH" >&2
  exit 1
fi
SIGNATURE="$(cat "$SIG_PATH")"

# ---------------------------------------------------------------------------
# Build manifest
# ---------------------------------------------------------------------------
echo "[2/4] building manifest at /tmp/${PLATFORM}.json ..."
NOTES_CONTENT="$(cat "$NOTES_FILE")"
MANIFEST_LOCAL="/tmp/errorta-${PLATFORM}-${TAG}.json"

python3 - <<PY > "$MANIFEST_LOCAL"
import json, sys
tag = ${TAG@Q}
notes = ${NOTES_CONTENT@Q}
pub_date = ${PUB_DATE@Q}
platform = ${PLATFORM@Q}
signature = ${SIGNATURE@Q}
url = ${ARTIFACT_URL@Q}
version = tag.lstrip("v")
print(json.dumps({
    "version": version,
    "notes": notes,
    "pub_date": pub_date,
    "platforms": {
        platform: {
            "signature": signature,
            "url": url,
        }
    }
}, indent=2))
PY

# ---------------------------------------------------------------------------
# Push manifest to errorta-downloads
# ---------------------------------------------------------------------------
WORKDIR="/tmp/errorta-downloads-${TAG}-${PLATFORM}"
echo "[3/4] cloning $REPO into $WORKDIR ..."
rm -rf "$WORKDIR"
gh repo clone "$REPO" "$WORKDIR"
mkdir -p "$WORKDIR/$(dirname "$MANIFEST_PATH")"
cp "$MANIFEST_LOCAL" "$WORKDIR/$MANIFEST_PATH"
(
  cd "$WORKDIR"
  git add "$MANIFEST_PATH"
  if git diff --cached --quiet; then
    echo "  no manifest changes vs upstream — skipping commit"
  else
    git commit -m "publish: $TAG $PLATFORM ($CHANNEL)"
    git push
  fi
)

# ---------------------------------------------------------------------------
# Upload .sig to the GitHub Release
# ---------------------------------------------------------------------------
echo "[4/4] uploading $SIG_PATH to release $TAG on $REPO ..."
DRAFT_FLAG=""
if [[ $DRAFT -eq 1 ]]; then
  DRAFT_FLAG="--draft"
fi
# Ensure the release exists; if not, create it as draft first.
if ! gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  gh release create "$TAG" --repo "$REPO" $DRAFT_FLAG \
    --title "$TAG" --notes-file "$NOTES_FILE"
fi
gh release upload "$TAG" --repo "$REPO" --clobber "$SIG_PATH"

echo
echo "Done."
echo "  manifest pushed: ${REPO}@${MANIFEST_PATH}"
echo "  signature uploaded: ${TAG} <- $(basename "$SIG_PATH")"
echo "  manifest preview:"
sed 's/^/    /' "$MANIFEST_LOCAL"
