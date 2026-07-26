#!/usr/bin/env bash
# Spec 19 Item 2 — the ONE supported way to change the Python lineage version.
#
# The Python lineage (sidecar + CLI) declares its version in THREE places that
# must never disagree: python/pyproject.toml (canonical — release-cli.sh seds it
# for the git tag, the tarball name and the Homebrew formula), and the two
# runtime mirrors errorta_app.__version__ / errorta_cli.__version__ (what
# /healthz, /version and `errorta status` self-report). Hand-editing one of them
# is how 0.1.0-alpha.11 shipped a binary that called itself alpha.10.
#
# Usage:
#   bash scripts/bump-version.sh 0.1.0-alpha.12 [--allow-dirty] [--help]
#
# Rewrites all three, prints a diff, and stops. It deliberately does NOT commit
# or tag — a bump is reviewed like any other change. Running it twice with the
# same value is a no-op. python/tests/test_version_identity.py locks the result,
# and scripts/release-cli.sh's preflight refuses to build on drift.
#
# NOT in scope: the desktop app lineage (package.json / Cargo.toml /
# tauri.conf.json) — it ships on a separate train owned by release-macos.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYPROJECT="$REPO_ROOT/python/pyproject.toml"
APP_INIT="$REPO_ROOT/python/errorta_app/__init__.py"
CLI_INIT="$REPO_ROOT/python/errorta_cli/__init__.py"

usage() {
  cat <<'EOF'
Usage: bump-version.sh <version> [--allow-dirty]

Rewrites the three Python version declarations to <version>:
  python/pyproject.toml            version = "..."      (canonical)
  python/errorta_app/__init__.py   __version__ = "..."  (mirror)
  python/errorta_cli/__init__.py   __version__ = "..."  (mirror)

<version> must look like  X.Y.Z  or  X.Y.Z-(alpha|beta|rc).N   e.g. 0.1.0-alpha.12

Options:
  --allow-dirty   Proceed even if the three files have uncommitted changes.
  --help          Show this help.

Does not commit and does not tag. Verify with:
  python/.venv/bin/python -m pytest python/tests/test_version_identity.py -q
  bash scripts/release-cli.sh --check
EOF
}

log() { printf '[bump-version] %s\n' "$*"; }
die() { echo "[bump-version] ERROR: $*" >&2; exit 1; }

# --- args ---
NEW_VERSION=""
ALLOW_DIRTY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    -*)            echo "[bump-version] unknown argument: $1" >&2; usage >&2; exit 2 ;;
    *)
      [[ -z "$NEW_VERSION" ]] || { echo "[bump-version] unexpected extra argument: $1" >&2; usage >&2; exit 2; }
      NEW_VERSION="$1"; shift ;;
  esac
done

[[ -n "$NEW_VERSION" ]] || { echo "[bump-version] a version is required." >&2; usage >&2; exit 2; }

# Malformed versions are refused up front: a typo here would otherwise be
# stamped into a git tag, a tarball name and a published Homebrew formula.
if [[ ! "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-(alpha|beta|rc)\.[0-9]+)?$ ]]; then
  die "malformed version '$NEW_VERSION' — expected X.Y.Z or X.Y.Z-(alpha|beta|rc).N (e.g. 0.1.0-alpha.12)."
fi

for f in "$PYPROJECT" "$APP_INIT" "$CLI_INIT"; do
  [[ -f "$f" ]] || die "missing declaration file: ${f#"$REPO_ROOT"/}"
done

# --- read the current values (same regex release-cli.sh:202 uses) ---
read_decl() { # <file> <lhs>  -> the first  <lhs> = "value"  literal
  sed -n "s/^[[:space:]]*$2[[:space:]]*=[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$1" | head -1
}

CUR_PROJ="$(read_decl "$PYPROJECT" version)"
CUR_APP="$(read_decl "$APP_INIT" __version__)"
CUR_CLI="$(read_decl "$CLI_INIT" __version__)"

[[ -n "$CUR_PROJ" ]] || die "could not read version = \"...\" from python/pyproject.toml."
[[ -n "$CUR_APP"  ]] || die "could not read __version__ = \"...\" from python/errorta_app/__init__.py."
[[ -n "$CUR_CLI"  ]] || die "could not read __version__ = \"...\" from python/errorta_cli/__init__.py."

log "current: pyproject=$CUR_PROJ  errorta_app=$CUR_APP  errorta_cli=$CUR_CLI"
log "target:  $NEW_VERSION"

# Idempotent: already at the target with all three agreeing -> nothing to do.
if [[ "$CUR_PROJ" == "$NEW_VERSION" && "$CUR_APP" == "$NEW_VERSION" && "$CUR_CLI" == "$NEW_VERSION" ]]; then
  log "already at $NEW_VERSION in all three declarations — no changes."
  exit 0
fi

# Refuse to bump on top of unrelated uncommitted edits to these exact files:
# the printed diff would mix the bump with someone else's work-in-progress.
if [[ $ALLOW_DIRTY -eq 0 ]] && git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  DIRTY="$(git -C "$REPO_ROOT" status --porcelain -- "$PYPROJECT" "$APP_INIT" "$CLI_INIT")"
  if [[ -n "$DIRTY" ]]; then
    echo "$DIRTY" >&2
    die "the version declaration files have uncommitted changes (above). Commit or stash them first, or pass --allow-dirty."
  fi
fi

# --- rewrite (portable: sed to a temp file, then move; no GNU/BSD -i split) ---
BACKUP_DIR="$(mktemp -d)"
trap 'rm -rf "$BACKUP_DIR"' EXIT

rewrite() { # <file> <lhs> <slot>
  # <slot> is a unique key, NOT the basename: two of the three declarations are
  # named `__init__.py`, so a basename-keyed backup collides and the printed diff
  # shows the wrong "before" file.
  local file="$1" lhs="$2" slot="$3" tmp
  cp "$file" "$BACKUP_DIR/$slot.orig"
  tmp="$BACKUP_DIR/$slot.new"
  # Only the FIRST matching declaration is rewritten (awk state flag), so a
  # later `version = "..."` in another table is never clobbered.
  awk -v lhs="$lhs" -v new="$NEW_VERSION" '
    !done && $0 ~ ("^[ \t]*" lhs "[ \t]*=[ \t]*\"[^\"]*\"") {
      sub(/"[^"]*"/, "\"" new "\"")
      done = 1
    }
    { print }
  ' "$file" > "$tmp"
  mv "$tmp" "$file"
}

rewrite "$PYPROJECT" version     pyproject
rewrite "$APP_INIT"  __version__ app_init
rewrite "$CLI_INIT"  __version__ cli_init

# --- verify the rewrite actually landed, then show the diff ---
echo
for triple in "$PYPROJECT|version|pyproject" "$APP_INIT|__version__|app_init" \
              "$CLI_INIT|__version__|cli_init"; do
  IFS='|' read -r f lhs slot <<<"$triple"
  got="$(read_decl "$f" "$lhs")"
  [[ "$got" == "$NEW_VERSION" ]] \
    || die "rewrite of ${f#"$REPO_ROOT"/} did not take (reads '$got', expected '$NEW_VERSION')."
  # `diff` exits 1 when files differ — expected here, so don't trip `set -e`.
  diff -u --label "a/${f#"$REPO_ROOT"/}" --label "b/${f#"$REPO_ROOT"/}" \
       "$BACKUP_DIR/$slot.orig" "$f" || true
done
echo

log "bumped $CUR_PROJ -> $NEW_VERSION in all three declarations."
log "NOT committed and NOT tagged — review, then commit yourself."
log "verify: python/.venv/bin/python -m pytest python/tests/test_version_identity.py -q"
log "verify: bash scripts/release-cli.sh --check"
