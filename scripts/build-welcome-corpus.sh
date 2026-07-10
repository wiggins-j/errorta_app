#!/usr/bin/env bash
# Build a reproducible welcome-corpus tarball for Errorta's F007 onboarding.
#
# Local-only flow: bundles a curated source-doc set into
# dist/welcome-corpus.tar.gz, prints version/sha256/bytes.
#
# Usage:
#   bash scripts/build-welcome-corpus.sh [--help]
#                                        [--output-dir <path>]
#                                        [--source-doc <path>] ...
#
# Refs: docs/specs/F-INFRA-11-welcome-corpus-tarball.md
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_PREFIX="[build-welcome-corpus]"
PIN_FILE="$REPO_ROOT/python/errorta_welcome/pinned_hash.json"

usage() {
  cat <<EOF
Usage: bash scripts/build-welcome-corpus.sh [options]

Options:
  --help                          Show this help and exit.
  --output-dir <path>             Override output directory (default: \$REPO_ROOT/dist).
  --source-doc <path>             Use only the supplied source docs (repeatable).
                                  If absent, the script bundles the default doc set.
  --verify                        After build, compare produced SHA-256 against
                                  errorta_welcome/pinned_hash.json. Exit 0 on match,
                                  exit 1 on mismatch.
  --publish <vMAJOR.MINOR.PATCH>  Draft a GitHub release on wiggins-j/errorta-downloads
                                  (tag welcome-corpus-<tag>). Requires gh CLI + auth.

Prints a three-line trailer on success:
  version: <version>
  sha256:  <64 hex chars>
  bytes:   <integer>
EOF
}

# -------- flag parsing --------
OUTPUT_DIR=""
declare -a SOURCE_DOCS=()
VERIFY=0
PUBLISH_TAG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help)
      usage
      exit 0
      ;;
    --output-dir)
      OUTPUT_DIR="${2:-}"
      if [[ -z "$OUTPUT_DIR" ]]; then
        echo "$LOG_PREFIX FATAL: --output-dir requires a path" >&2
        exit 1
      fi
      shift 2
      ;;
    --source-doc)
      if [[ -z "${2:-}" ]]; then
        echo "$LOG_PREFIX FATAL: --source-doc requires a path" >&2
        exit 1
      fi
      SOURCE_DOCS+=("$2")
      shift 2
      ;;
    --verify)
      VERIFY=1
      shift
      ;;
    --publish)
      PUBLISH_TAG="${2:-}"
      if [[ -z "$PUBLISH_TAG" ]]; then
        echo "$LOG_PREFIX FATAL: --publish requires a tag of the form vMAJOR.MINOR.PATCH (got )" >&2
        exit 1
      fi
      shift 2
      ;;
    *)
      echo "$LOG_PREFIX FATAL: unknown flag: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

# Validate --publish tag shape + gh CLI before doing any work.
if [[ -n "$PUBLISH_TAG" ]]; then
  if ! [[ "$PUBLISH_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "$LOG_PREFIX FATAL: --publish requires a tag of the form vMAJOR.MINOR.PATCH (got $PUBLISH_TAG)" >&2
    exit 1
  fi
  if ! command -v gh >/dev/null 2>&1; then
    echo "$LOG_PREFIX FATAL: gh CLI not found. Install: brew install gh" >&2
    exit 1
  fi
  if ! gh auth status >/dev/null 2>&1; then
    echo "$LOG_PREFIX FATAL: gh not authenticated. Run: gh auth login" >&2
    exit 1
  fi
fi

# Default OUTPUT_DIR. For --verify with no explicit dir, use a tmp dir so
# the verify path does not clobber dist/ with a non-pinned tarball.
if [[ -z "$OUTPUT_DIR" ]]; then
  if [[ "$VERIFY" -eq 1 ]]; then
    OUTPUT_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t welcome-corpus)"
    trap 'rm -rf "$OUTPUT_DIR"' EXIT
  else
    OUTPUT_DIR="$REPO_ROOT/dist"
  fi
fi
mkdir -p "$OUTPUT_DIR"

# -------- read pin file (for max_bytes + version) --------
if [[ ! -f "$PIN_FILE" ]]; then
  echo "$LOG_PREFIX FATAL: pin file not found at $PIN_FILE" >&2
  exit 1
fi

MAX_BYTES="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["max_bytes"])' "$PIN_FILE")"
PIN_VERSION="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$PIN_FILE")"
PIN_SHA256="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["sha256"])' "$PIN_FILE")"

# -------- compute sha256 helper --------
sha256_of_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    echo "$LOG_PREFIX FATAL: neither shasum nor sha256sum found on PATH" >&2
    return 1
  fi
}

# -------- staging tree --------
STAGE_ROOT="$OUTPUT_DIR/welcome-corpus-build"
STAGE_TOP="$STAGE_ROOT/welcome-corpus"
STAGE_DOCS="$STAGE_TOP/docs"
rm -rf "$STAGE_ROOT"
mkdir -p "$STAGE_DOCS"

# -------- resolve source-doc set --------
declare -a STAGED_RELPATHS=()

stage_verbatim() {
  local src="$1"
  local rel="$2"
  local dest="$STAGE_TOP/$rel"
  mkdir -p "$(dirname "$dest")"
  cp "$src" "$dest"
  STAGED_RELPATHS+=("$rel")
}

stage_spec_subset() {
  # Keep everything before the first line matching '^## Technical approach$'.
  local src="$1"
  local rel="$2"
  local dest="$STAGE_TOP/$rel"
  if ! grep -q "^## Technical approach$" "$src"; then
    echo "$LOG_PREFIX FATAL: subsetter could not locate '## Technical approach' header in $src. Has the spec template changed? Update scripts/build-welcome-corpus.sh or curate manually with --source-doc." >&2
    exit 1
  fi
  mkdir -p "$(dirname "$dest")"
  awk '/^## Technical approach$/ {exit} {print}' "$src" > "$dest"
  STAGED_RELPATHS+=("$rel")
}

stage_north_star_subset() {
  # Special-case: keep NORTH_STAR through the end of the 'Non-goals' section
  # (stops at the next '^## ' header following '^## Non-goals$'). Resolved
  # 2026-06-08 per the F-INFRA-11 plan's stop-here-and-ask gate.
  local src="$1"
  local rel="$2"
  local dest="$STAGE_TOP/$rel"
  if ! grep -q "^## Non-goals$" "$src"; then
    echo "$LOG_PREFIX FATAL: NORTH_STAR subsetter could not locate '## Non-goals' header in $src." >&2
    exit 1
  fi
  mkdir -p "$(dirname "$dest")"
  awk '
    /^## / {
      if (in_nongoals && $0 != "## Non-goals") { exit }
      if ($0 == "## Non-goals") { in_nongoals = 1 }
    }
    { print }
  ' "$src" > "$dest"
  STAGED_RELPATHS+=("$rel")
}

if [[ ${#SOURCE_DOCS[@]} -gt 0 ]]; then
  for src in "${SOURCE_DOCS[@]}"; do
    if [[ ! -f "$src" ]]; then
      echo "$LOG_PREFIX FATAL: --source-doc file not found: $src" >&2
      exit 1
    fi
    base="$(basename "$src")"
    stage_verbatim "$src" "docs/$base"
  done
else
  NORTH_STAR="$REPO_ROOT/docs/NORTH_STAR.md"
  F001_SPEC="$REPO_ROOT/docs/specs/F001-judge-and-grounding-loop.md"
  F004_SPEC="$REPO_ROOT/docs/specs/F004-corpus-drag-and-drop.md"
  WC_SRC="$REPO_ROOT/docs/welcome-corpus-src"

  if [[ -d "$WC_SRC" && -f "$WC_SRC/03-built-on-aiar.md" ]]; then
    # Slice (d) source set: subsetted specs + hand-authored 1-pagers.
    stage_north_star_subset "$NORTH_STAR" "docs/00-what-is-errorta.md"
    stage_spec_subset "$F001_SPEC" "docs/01-the-judge-loop.md"
    stage_spec_subset "$F004_SPEC" "docs/02-corpora-and-rag.md"
    stage_verbatim "$WC_SRC/03-built-on-aiar.md" "docs/03-built-on-aiar.md"
    stage_verbatim "$WC_SRC/04-faq.md" "docs/04-faq.md"
    stage_verbatim "$WC_SRC/05-how-to-add-your-own-files.md" "docs/05-how-to-add-your-own-files.md"
  else
    # Slice (a) interim fallback if welcome-corpus-src/ is missing.
    stage_verbatim "$NORTH_STAR" "docs/00-what-is-errorta.md"
    stage_verbatim "$F001_SPEC" "docs/01-the-judge-loop.md"
    stage_verbatim "$F004_SPEC" "docs/02-corpora-and-rag.md"
    echo "TODO: replaced in F-INFRA-11 slice (d)" > "$STAGE_DOCS/_placeholder-built-on-aiar.md"
    STAGED_RELPATHS+=("docs/_placeholder-built-on-aiar.md")
  fi
fi

# -------- generate manifest.json (timestamps tied to source commit for stability) --------
SOURCE_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
SOURCE_EPOCH="$(git -C "$REPO_ROOT" log -1 --format=%ct 2>/dev/null || echo 0)"
GENERATED_AT="$(python3 -c 'import sys,datetime; print(datetime.datetime.fromtimestamp(int(sys.argv[1]), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$SOURCE_EPOCH")"

python3 - "$STAGE_TOP/manifest.json" "$PIN_VERSION" "$GENERATED_AT" "$SOURCE_COMMIT" "${STAGED_RELPATHS[@]}" <<'PYEOF'
import json
import sys
out_path = sys.argv[1]
version = sys.argv[2]
generated_at = sys.argv[3]
source_commit = sys.argv[4]
files = sorted(sys.argv[5:])
with open(out_path, "w") as f:
    json.dump(
        {
            "version": version,
            "files": files,
            "generated_at": generated_at,
            "source_commit": source_commit,
        },
        f,
        indent=2,
        sort_keys=True,
    )
    f.write("\n")
PYEOF

# -------- byte-stable tarball via Python tarfile --------
# Using Python's tarfile module gives us deterministic mtime/uid/gid/uname/gname
# control on both BSD tar (macOS default) and GNU tar (Linux).
TARBALL="$OUTPUT_DIR/welcome-corpus.tar.gz"

python3 - "$STAGE_ROOT" "$TARBALL" "$SOURCE_EPOCH" <<'PYEOF'
import gzip
import os
import sys
import tarfile

stage_root = sys.argv[1]
out_path = sys.argv[2]
epoch = int(sys.argv[3])

def _reset(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    ti.mtime = epoch
    ti.uid = 0
    ti.gid = 0
    ti.uname = ""
    ti.gname = ""
    if ti.isdir():
        ti.mode = 0o755
    else:
        ti.mode = 0o644
    return ti

files_to_add = []
for root, dirs, files in os.walk(os.path.join(stage_root, "welcome-corpus")):
    dirs.sort()
    files.sort()
    rel_root = os.path.relpath(root, stage_root)
    files_to_add.append(rel_root)
    for f in files:
        files_to_add.append(os.path.join(rel_root, f))

with open(out_path, "wb") as raw:
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tf:
            for rel in files_to_add:
                abs_path = os.path.join(stage_root, rel)
                tf.add(abs_path, arcname=rel, recursive=False, filter=_reset)
PYEOF

# -------- compute hash + size, enforce cap --------
PRODUCED_SHA256="$(sha256_of_file "$TARBALL")"
PRODUCED_BYTES="$(wc -c < "$TARBALL" | tr -d ' ')"

if [[ "$PRODUCED_BYTES" -gt "$MAX_BYTES" ]]; then
  echo "$LOG_PREFIX FATAL: produced tarball is $PRODUCED_BYTES bytes; cap is $MAX_BYTES bytes" >&2
  exit 1
fi

# -------- print trailer --------
echo "version: $PIN_VERSION"
echo "sha256:  $PRODUCED_SHA256"
echo "bytes:   $PRODUCED_BYTES"

# -------- --verify --------
if [[ "$VERIFY" -eq 1 ]]; then
  if [[ "$PRODUCED_SHA256" == "$PIN_SHA256" ]]; then
    echo "$LOG_PREFIX VERIFY OK: sha256 matches pin ($PRODUCED_SHA256)"
    exit 0
  else
    echo "$LOG_PREFIX VERIFY FAIL"
    echo "  produced: $PRODUCED_SHA256"
    echo "  pinned:   $PIN_SHA256"
    exit 1
  fi
fi

# -------- --publish --------
if [[ -n "$PUBLISH_TAG" ]]; then
  SHA_FILE="$OUTPUT_DIR/SHA256SUMS.txt"
  NOTES_FILE="$OUTPUT_DIR/release-notes.md"

  echo "$PRODUCED_SHA256  welcome-corpus.tar.gz" > "$SHA_FILE"

  cat > "$NOTES_FILE" <<NOTESEOF
# welcome-corpus-$PUBLISH_TAG

Welcome-corpus tarball for Errorta's first-run onboarding.

- Source commit: $SOURCE_COMMIT
- Generated: $GENERATED_AT
- SHA-256: \`$PRODUCED_SHA256\`
- Bytes: $PRODUCED_BYTES

Verify with:

\`\`\`
shasum -a 256 -c SHA256SUMS.txt
\`\`\`
NOTESEOF

  gh release create "welcome-corpus-$PUBLISH_TAG" \
    --repo wiggins-j/errorta-downloads \
    --title "welcome-corpus $PUBLISH_TAG" \
    --notes-file "$NOTES_FILE" \
    --draft \
    "$TARBALL" \
    "$SHA_FILE"

  echo "$LOG_PREFIX DRAFT RELEASE created: welcome-corpus-$PUBLISH_TAG"
  echo "$LOG_PREFIX NEXT: visit https://github.com/wiggins-j/errorta-downloads/releases and un-draft after spot-checking. Then hand-edit python/errorta_welcome/pinned_hash.json."
fi
