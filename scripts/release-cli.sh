#!/usr/bin/env bash
# F148 S1 — per-platform CLI release pipeline (Homebrew path).
#
# Builds the self-contained `errorta` CLI binary for THIS host's OS/arch,
# (macOS) signs + notarizes it, tarballs it, uploads it to the errorta_app
# GitHub Release, and updates the Homebrew tap formula. GitHub Actions is OFF
# (locked decision) — this runs locally on the maintainer's hardware, once per
# platform (macOS arm64, macOS x86_64 or universal2, Linux x86_64).
#
# Usage:
#   bash scripts/release-cli.sh [--version X.Y.Z] [--tap-dir PATH] [--push-tap]
#                               [--skip-notarize] [--dry-run] [--help]
#
# Version is read from python/pyproject.toml (the single source) unless
# --version is given. See docs/BUILD_AND_RELEASE.md for the full runbook and
# docs/SIGNING_MACOS.md for signing/notarization credential setup.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --- constants ---
GH_REPO="wiggins-j/errorta_app"          # release assets are hosted here
TEMPLATE="$REPO_ROOT/scripts/homebrew/errorta.rb.template"
ENTITLEMENTS="$REPO_ROOT/src-tauri/macos/entitlements.plist"
NOTARIZE_LIB="$REPO_ROOT/scripts/lib/notarize.sh"

usage() {
  cat <<'EOF'
Usage: release-cli.sh [options]

Builds + releases the `errorta` CLI binary for the current host platform.

Options:
  --version X.Y.Z   Override the version (default: read from python/pyproject.toml).
  --tap-dir PATH    Local clone of errorta/homebrew-tap. Its Formula/errorta.rb
                    is regenerated with this platform's url + sha256 (other
                    platforms' values are preserved). Omit to skip formula work.
  --push-tap        After rendering the formula, git commit + push the tap.
                    Requires --tap-dir. Omit to leave the change uncommitted.
  --skip-notarize   Skip macOS codesign + notarization (produces an unsigned
                    binary — for local testing only; auto-skipped on Linux).
  --dry-run         Print every step without building, uploading, or pushing.
  --help            Show this help.

Prerequisites:
  - pyinstaller in python/.venv (pip install -e python[dev]) or on PATH.
  - macOS: a Developer ID identity in APPLE_SIGNING_IDENTITY (or
    ~/.config/errorta-release.env) + notarization creds — see docs/SIGNING_MACOS.md.
  - gh CLI authenticated (gh auth login) for the upload step.
EOF
}

# --- args ---
VERSION=""
TAP_DIR=""
PUSH_TAP=0
SKIP_NOTARIZE=0
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)      VERSION="${2:?--version needs a value}"; shift 2 ;;
    --version=*)    VERSION="${1#*=}"; shift ;;
    --tap-dir)      TAP_DIR="${2:?--tap-dir needs a value}"; shift 2 ;;
    --tap-dir=*)    TAP_DIR="${1#*=}"; shift ;;
    --push-tap)     PUSH_TAP=1; shift ;;
    --skip-notarize) SKIP_NOTARIZE=1; shift ;;
    --dry-run)      DRY_RUN=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "[release-cli] unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ $PUSH_TAP -eq 1 && -z "$TAP_DIR" ]]; then
  echo "[release-cli] --push-tap requires --tap-dir." >&2
  exit 2
fi

log()  { printf '[release-cli] %s\n' "$*"; }
step() { printf '\n[release-cli] == %s ==\n' "$*"; }
die()  { echo "[release-cli] ERROR: $*" >&2; exit 1; }

# --- resolve version (single source: python/pyproject.toml) ---
if [[ -z "$VERSION" ]]; then
  VERSION="$(sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
              "$REPO_ROOT/python/pyproject.toml" | head -1)"
  [[ -n "$VERSION" ]] || die "could not read version from python/pyproject.toml (pass --version)."
fi
TAG="cli-v${VERSION}"

# --- resolve host OS/arch ---
case "$(uname -s)" in
  Darwin) OS="darwin" ;;
  Linux)  OS="linux" ;;
  *) die "unsupported OS '$(uname -s)' (Homebrew targets macOS + Linux only)." ;;
esac
case "$(uname -m)" in
  arm64|aarch64) ARCH="arm64" ;;
  x86_64|amd64)  ARCH="x86_64" ;;
  *) die "unsupported arch '$(uname -m)'." ;;
esac

# On Linux, notarization is a no-op (unsigned community-tier, matches the app).
if [[ "$OS" == "linux" ]]; then SKIP_NOTARIZE=1; fi

BINARY="$REPO_ROOT/dist/errorta"
TARBALL_NAME="errorta-${VERSION}-${OS}-${ARCH}.tar.gz"
TARBALL="$REPO_ROOT/dist/${TARBALL_NAME}"

# Deterministic asset URL for any platform (derived from version + tag).
asset_url() { # <os> <arch>
  echo "https://github.com/${GH_REPO}/releases/download/${TAG}/errorta-${VERSION}-$1-$2.tar.gz"
}

log "version:   $VERSION"
log "tag:       $TAG"
log "platform:  $OS/$ARCH"
log "binary:    $BINARY"
log "tarball:   $TARBALL"
log "gh repo:   $GH_REPO"
[[ $DRY_RUN -eq 1 ]] && log "MODE:      dry-run (no build / upload / push)"

# ---------------------------------------------------------------------------
# 1. Build the binary with PyInstaller.
# ---------------------------------------------------------------------------
step "build (pyinstaller python/cli.spec)"
if [[ -x "$REPO_ROOT/python/.venv/bin/pyinstaller" ]]; then
  PYINSTALLER="$REPO_ROOT/python/.venv/bin/pyinstaller"
elif command -v pyinstaller >/dev/null 2>&1; then
  PYINSTALLER="pyinstaller"
else
  PYINSTALLER=""
fi

# Export the signing env the spec honors (python/cli.spec reads
# ERRORTA_CODESIGN_IDENTITY / ERRORTA_ENTITLEMENTS_PLIST) so PyInstaller signs
# the onefile during assembly; we re-sign explicitly below as the authority.
if [[ "$OS" == "darwin" && $SKIP_NOTARIZE -eq 0 ]]; then
  ENV_FILE="${HOME}/.config/errorta-release.env"
  if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
  fi
  if [[ -z "${APPLE_SIGNING_IDENTITY:-}" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      log "[dry-run] APPLE_SIGNING_IDENTITY not set — using a placeholder for the preview (docs/SIGNING_MACOS.md)."
      APPLE_SIGNING_IDENTITY="<APPLE_SIGNING_IDENTITY>"
    else
      die "APPLE_SIGNING_IDENTITY not set (env or ~/.config/errorta-release.env) — see docs/SIGNING_MACOS.md."
    fi
  fi
  export ERRORTA_CODESIGN_IDENTITY="${ERRORTA_CODESIGN_IDENTITY:-$APPLE_SIGNING_IDENTITY}"
  export ERRORTA_ENTITLEMENTS_PLIST="$ENTITLEMENTS"
fi

if [[ $DRY_RUN -eq 1 ]]; then
  log "[dry-run] ${PYINSTALLER:-pyinstaller} --noconfirm --clean --distpath dist python/cli.spec"
  log "[dry-run] verify: dist/errorta --help"
else
  [[ -n "$PYINSTALLER" ]] \
    || die "pyinstaller not found. Activate python/.venv or 'pip install -e python[dev]' (see docs/BUILD_AND_RELEASE.md)."
  log "using $PYINSTALLER"
  "$PYINSTALLER" --noconfirm --clean --distpath "$REPO_ROOT/dist" \
                 --workpath "$REPO_ROOT/build/cli" "$REPO_ROOT/python/cli.spec"
  [[ -f "$BINARY" ]] || die "expected binary not produced at $BINARY."
  log "smoke-test: $BINARY --help"
  "$BINARY" --help >/dev/null || die "$BINARY --help failed to run."
fi

# ---------------------------------------------------------------------------
# 2. macOS: codesign (Developer ID + hardened runtime) + notarize.
#    NOTE: a bare Mach-O binary CANNOT be stapled (stapling only works for
#    .app/.dmg/.pkg). We zip the signed binary and submit the zip to notarytool;
#    Gatekeeper then verifies notarization ONLINE on first run of the extracted
#    binary. There is intentionally no `stapler staple` here.
# ---------------------------------------------------------------------------
if [[ "$OS" == "darwin" && $SKIP_NOTARIZE -eq 0 ]]; then
  step "codesign + notarize (bare binary; notarize-not-staple)"
  # shellcheck source=scripts/lib/notarize.sh
  source "$NOTARIZE_LIB"

  if [[ $DRY_RUN -eq 1 ]]; then
    log "[dry-run] verify identity present: security find-identity -v -p codesigning | grep '$APPLE_SIGNING_IDENTITY'"
    log "[dry-run] codesign --force --timestamp --options runtime --entitlements '$ENTITLEMENTS' --sign '$APPLE_SIGNING_IDENTITY' $BINARY"
    log "[dry-run] codesign --verify --strict $BINARY"
    log "[dry-run] zip signed binary -> submit to notarytool --wait (via _notary_submit); NO staple (bare Mach-O)"
  else
    security find-identity -v -p codesigning | grep -F "$APPLE_SIGNING_IDENTITY" >/dev/null \
      || die "signing identity not in codesigning keychain: $APPLE_SIGNING_IDENTITY (see docs/SIGNING_MACOS.md)."
    [[ -z "$(_notary_creds_mode)" ]] \
      && die "no notarization credentials — set up the '$ERRORTA_NOTARY_PROFILE' keychain profile or APPLE_ID/APPLE_TEAM_ID/APPLE_APP_SPECIFIC_PASSWORD (docs/SIGNING_MACOS.md)."

    log "codesigning $BINARY (Developer ID, hardened runtime)"
    codesign --force --timestamp --options runtime \
             --entitlements "$ENTITLEMENTS" \
             --sign "$APPLE_SIGNING_IDENTITY" "$BINARY"
    codesign --verify --strict "$BINARY"

    NOTARY_TMP="$(mktemp -d)"
    NOTARY_ZIP="$NOTARY_TMP/errorta.zip"
    # -j junks paths so the archive holds just `errorta`.
    /usr/bin/zip -j -q "$NOTARY_ZIP" "$BINARY"
    log "submitting to notarytool (bare binary is not stapleable — online check only)"
    if ! _notary_submit "$NOTARY_ZIP"; then
      rm -rf "$NOTARY_TMP"
      die "notarization was not Accepted (see the notarytool log above)."
    fi
    rm -rf "$NOTARY_TMP"
    log "notarized OK (no staple — Gatekeeper verifies online for a standalone binary)."
  fi
elif [[ "$OS" == "darwin" && $SKIP_NOTARIZE -eq 1 ]]; then
  step "codesign + notarize"
  log "SKIPPED (--skip-notarize): the binary is unsigned; Gatekeeper will quarantine a downloaded copy. Local testing only."
else
  step "codesign + notarize"
  log "SKIPPED on Linux (unsigned community-tier, matches the app posture)."
fi

# ---------------------------------------------------------------------------
# 3. Tarball the (signed) binary + compute sha256.
# ---------------------------------------------------------------------------
step "package tarball + sha256"
SHA256=""
if [[ $DRY_RUN -eq 1 ]]; then
  log "[dry-run] tar -czf dist/$TARBALL_NAME -C dist errorta"
  log "[dry-run] shasum -a 256 dist/$TARBALL_NAME"
  SHA256="<sha256-computed-after-build>"
else
  ( cd "$REPO_ROOT/dist" && tar -czf "$TARBALL_NAME" errorta )
  [[ -f "$TARBALL" ]] || die "tarball not produced at $TARBALL."
  SHA256="$(shasum -a 256 "$TARBALL" | awk '{print $1}')"
  log "tarball:   $TARBALL"
  log "sha256:    $SHA256"
fi

# ---------------------------------------------------------------------------
# 4. Upload to the errorta_app GitHub Release for TAG.
# ---------------------------------------------------------------------------
step "upload to GitHub Release ($TAG on $GH_REPO)"
if [[ $DRY_RUN -eq 1 ]]; then
  log "[dry-run] gh auth status"
  log "[dry-run] gh release create $TAG dist/$TARBALL_NAME --repo $GH_REPO --title 'errorta CLI $VERSION' --notes ... \\"
  log "[dry-run]   || gh release upload $TAG dist/$TARBALL_NAME --repo $GH_REPO --clobber"
else
  command -v gh >/dev/null 2>&1 || die "gh CLI not found (https://cli.github.com/)."
  gh auth status >/dev/null 2>&1 || die "gh is not authenticated — run 'gh auth login'."
  REL_NOTES="errorta CLI ${VERSION} — self-contained binary (embeds sidecar + AIAR; ~100-200 MB).
Install: brew install errorta/tap/errorta"
  if gh release create "$TAG" "$TARBALL" \
        --repo "$GH_REPO" \
        --title "errorta CLI ${VERSION}" \
        --notes "$REL_NOTES" 2>/dev/null; then
    log "created release $TAG and uploaded $TARBALL_NAME."
  else
    log "release $TAG exists — uploading asset with --clobber."
    gh release upload "$TAG" "$TARBALL" --repo "$GH_REPO" --clobber \
      || die "gh release upload failed."
  fi
fi

# ---------------------------------------------------------------------------
# 5. Render the tap formula (this platform's url+sha; preserve the others).
# ---------------------------------------------------------------------------
if [[ -z "$TAP_DIR" ]]; then
  step "formula"
  log "SKIPPED (no --tap-dir). Re-run with --tap-dir <clone of errorta/homebrew-tap> to update the formula."
else
  step "render tap formula ($TAP_DIR/Formula/errorta.rb)"
  [[ -f "$TEMPLATE" ]] || die "formula template missing at $TEMPLATE."
  FORMULA_DIR="$TAP_DIR/Formula"
  FORMULA="$FORMULA_DIR/errorta.rb"

  # extract_sha <formula-file> <version-stamped-tarball-basename>
  # Finds the url line carrying that exact (version+arch) asset name, then the
  # sha256 on the following line. Empty if not present (e.g. new version, or an
  # arch not yet built) -> the placeholder is kept.
  extract_sha() {
    local file="$1" token="$2"
    [[ -f "$file" ]] || { echo ""; return 0; }
    awk -v tok="$token" '
      index($0, tok) { seen=1; next }
      seen && /sha256/ {
        if (match($0, /[0-9a-f]{64}/)) { print substr($0, RSTART, RLENGTH) }
        exit
      }
    ' "$file"
  }

  # This platform's fresh sha.
  D_ARM_SHA="@@DARWIN_ARM64_SHA@@"
  D_X86_SHA="@@DARWIN_X86_64_SHA@@"
  L_X86_SHA="@@LINUX_X86_64_SHA@@"

  # Preserve the OTHER platforms' shas from the current formula (matched by the
  # NEW version's asset name — an old-version formula won't match, correctly
  # leaving those as placeholders until their own runs land).
  EXIST_ARM="$(extract_sha "$FORMULA" "errorta-${VERSION}-darwin-arm64.tar.gz")"
  EXIST_X86="$(extract_sha "$FORMULA" "errorta-${VERSION}-darwin-x86_64.tar.gz")"
  EXIST_LNX="$(extract_sha "$FORMULA" "errorta-${VERSION}-linux-x86_64.tar.gz")"
  [[ -n "$EXIST_ARM" ]] && D_ARM_SHA="$EXIST_ARM"
  [[ -n "$EXIST_X86" ]] && D_X86_SHA="$EXIST_X86"
  [[ -n "$EXIST_LNX" ]] && L_X86_SHA="$EXIST_LNX"

  # Overwrite THIS platform's sha with the freshly computed value.
  case "${OS}-${ARCH}" in
    darwin-arm64)  D_ARM_SHA="$SHA256" ;;
    darwin-x86_64) D_X86_SHA="$SHA256" ;;
    linux-x86_64)  L_X86_SHA="$SHA256" ;;
    *) die "no formula slot for ${OS}-${ARCH}." ;;
  esac

  D_ARM_URL="$(asset_url darwin arm64)"
  D_X86_URL="$(asset_url darwin x86_64)"
  L_X86_URL="$(asset_url linux x86_64)"

  render_formula() {
    sed -e "s|@@VERSION@@|${VERSION}|g" \
        -e "s|@@DARWIN_ARM64_URL@@|${D_ARM_URL}|g" \
        -e "s|@@DARWIN_ARM64_SHA@@|${D_ARM_SHA}|g" \
        -e "s|@@DARWIN_X86_64_URL@@|${D_X86_URL}|g" \
        -e "s|@@DARWIN_X86_64_SHA@@|${D_X86_SHA}|g" \
        -e "s|@@LINUX_X86_64_URL@@|${L_X86_URL}|g" \
        -e "s|@@LINUX_X86_64_SHA@@|${L_X86_SHA}|g" \
        "$TEMPLATE"
  }

  if [[ $DRY_RUN -eq 1 ]]; then
    log "[dry-run] would write $FORMULA with:"
    log "[dry-run]   version=$VERSION  this=${OS}-${ARCH} sha=${SHA256}"
    log "[dry-run]   darwin-arm64 sha=${D_ARM_SHA}"
    log "[dry-run]   darwin-x86_64 sha=${D_X86_SHA}"
    log "[dry-run]   linux-x86_64 sha=${L_X86_SHA}"
    log "[dry-run] rendered formula preview:"
    render_formula | sed 's/^/    /'
  else
    [[ -d "$TAP_DIR/.git" ]] || die "--tap-dir '$TAP_DIR' is not a git clone of errorta/homebrew-tap."
    mkdir -p "$FORMULA_DIR"
    render_formula > "$FORMULA"
    log "wrote $FORMULA"
    if grep -q '@@' "$FORMULA"; then
      log "NOTE: some platform sha256 placeholders remain (that arch isn't built for $VERSION yet)."
    fi
  fi

  # ---- optional: commit + push the tap ----
  step "commit + push tap"
  if [[ $PUSH_TAP -eq 0 ]]; then
    log "SKIPPED (no --push-tap). Review $FORMULA then commit + push by hand, or re-run with --push-tap."
  elif [[ $DRY_RUN -eq 1 ]]; then
    log "[dry-run] git -C $TAP_DIR add Formula/errorta.rb"
    log "[dry-run] git -C $TAP_DIR commit -m 'errorta $VERSION ($OS/$ARCH)'"
    log "[dry-run] git -C $TAP_DIR push"
  else
    git -C "$TAP_DIR" add "Formula/errorta.rb"
    if git -C "$TAP_DIR" diff --cached --quiet; then
      log "no formula changes to commit."
    else
      git -C "$TAP_DIR" commit -m "errorta ${VERSION} (${OS}/${ARCH})"
      git -C "$TAP_DIR" push
      log "pushed tap update."
    fi
  fi
fi

step "done"
log "platform ${OS}/${ARCH} for errorta $VERSION complete."
[[ $DRY_RUN -eq 1 ]] && log "(dry-run — nothing was built, uploaded, or pushed.)"
log "Repeat on each platform (macOS arm64, macOS x86_64/universal2, Linux x86_64); see docs/BUILD_AND_RELEASE.md."
