#!/usr/bin/env bash
# Test the errorta Homebrew formula render + prune pass (F148 S1).
#
# Renders scripts/homebrew/errorta.rb.template with fake shas across the
# arch-matrix scenarios a real release walks through, runs the shared pruner
# (scripts/lib/prune-formula.awk), and asserts the result is a valid,
# publishable formula: no @@placeholder@@ tokens survive, the right arch blocks
# are kept/dropped, `ruby -c` parses it, and (advisory) `brew style` is clean
# when brew is available.
#
# No build, no network, no credentials — safe to run anywhere.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$REPO_ROOT/scripts/homebrew/errorta.rb.template"
PRUNE_AWK="$REPO_ROOT/scripts/lib/prune-formula.awk"
VERSION="9.9.9"
FAKE_SHA="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

pass=0; fail=0
ok()   { printf '  ok   %s\n' "$*"; pass=$((pass + 1)); }
bad()  { printf '  FAIL %s\n' "$*"; fail=$((fail + 1)); }

# render <arm_sha> <intel_sha> <linux_sha>  (use "@@" for an unbuilt arch)
render() {
  local a="$1" i="$2" l="$3"
  [[ "$a" == "@@" ]] && a="@@DARWIN_ARM64_SHA@@"
  [[ "$i" == "@@" ]] && i="@@DARWIN_X86_64_SHA@@"
  [[ "$l" == "@@" ]] && l="@@LINUX_X86_64_SHA@@"
  local base="https://github.com/wiggins-j/errorta_app/releases/download/cli-v${VERSION}"
  sed -e "s|@@VERSION@@|${VERSION}|g" \
      -e "s|@@DARWIN_ARM64_URL@@|${base}/errorta-${VERSION}-darwin-arm64.tar.gz|g" \
      -e "s|@@DARWIN_ARM64_SHA@@|${a}|g" \
      -e "s|@@DARWIN_X86_64_URL@@|${base}/errorta-${VERSION}-darwin-x86_64.tar.gz|g" \
      -e "s|@@DARWIN_X86_64_SHA@@|${i}|g" \
      -e "s|@@LINUX_X86_64_URL@@|${base}/errorta-${VERSION}-linux-x86_64.tar.gz|g" \
      -e "s|@@LINUX_X86_64_SHA@@|${l}|g" \
      "$TEMPLATE"
}

prune() { awk -v guard="${1:-}" -f "$PRUNE_AWK"; }

# assert_scenario <name> <guard> <arm> <intel> <linux> <expect-keywords-csv> <reject-keywords-csv>
assert_scenario() {
  local name="$1" guard="$2" arm="$3" intel="$4" linux="$5" expect="$6" reject="$7"
  local out kw
  out="$(render "$arm" "$intel" "$linux" | prune "$guard")"

  printf '\n[%s]\n' "$name"

  if grep -q '@@' <<<"$out"; then bad "$name: @@placeholder@@ survived prune"; else ok "no @@ placeholders remain"; fi

  if [[ -n "$expect" ]]; then
    IFS=',' read -ra WANT <<<"$expect"
    for kw in "${WANT[@]}"; do
      [[ -z "$kw" ]] && continue
      if grep -qF "$kw" <<<"$out"; then ok "keeps: $kw"; else bad "$name: expected but missing: $kw"; fi
    done
  fi
  if [[ -n "$reject" ]]; then
    IFS=',' read -ra NOPE <<<"$reject"
    for kw in "${NOPE[@]}"; do
      [[ -z "$kw" ]] && continue
      if grep -qF "$kw" <<<"$out"; then bad "$name: should have dropped: $kw"; else ok "drops: $kw"; fi
    done
  fi

  # ruby -c syntax (only meaningful with real-looking shas)
  if command -v ruby >/dev/null 2>&1; then
    if ruby -c <<<"$out" >/dev/null 2>&1; then ok "ruby -c parses"; else bad "$name: ruby -c failed"; fi
  fi

  # advisory: brew style if brew is present (never gates the test). Lint inside a
  # tap layout (<dir>/Formula/errorta.rb in a git repo) so Homebrew applies its
  # formula-specific rubocop config, not the generic Ruby cops it exempts for
  # formulae — otherwise a loose file reports spurious Sorbet/frozen-string nits.
  if command -v brew >/dev/null 2>&1; then
    local tap; tap="$(mktemp -d)"; mkdir -p "$tap/Formula"; git -C "$tap" init -q
    printf '%s\n' "$out" > "$tap/Formula/errorta.rb"
    if brew style "$tap/Formula/errorta.rb" >/dev/null 2>&1; then ok "brew style clean (advisory)"
    else printf '  (advisory) brew style reported issues (non-gating)\n'; fi
    rm -rf "$tap"
  fi
}

echo "== render/prune formula tests =="

# arm64-only (the v1 cut): darwin-intel + linux pruned, guard added, on_macos kept.
assert_scenario "arm64-only (v1)" "depends_on arch: :arm64" \
  "$FAKE_SHA" "@@" "@@" \
  'on_macos,on_arm,depends_on arch: :arm64,darwin-arm64.tar.gz' \
  'on_intel,on_linux,darwin-x86_64.tar.gz,linux-x86_64.tar.gz'

# full matrix: all three assets kept, no guard.
assert_scenario "full matrix" "" \
  "$FAKE_SHA" "$FAKE_SHA" "$FAKE_SHA" \
  'on_macos,on_linux,on_arm,on_intel,darwin-arm64.tar.gz,darwin-x86_64.tar.gz,linux-x86_64.tar.gz' \
  ''

# arm + linux (no darwin-intel): darwin-x86_64 dropped; on_macos kept (arm), linux kept.
assert_scenario "arm + linux" "" \
  "$FAKE_SHA" "@@" "$FAKE_SHA" \
  'on_macos,on_arm,on_linux,darwin-arm64.tar.gz,linux-x86_64.tar.gz' \
  'darwin-x86_64.tar.gz'

# linux-only: whole on_macos dropped (both darwin leaves placeholder), linux kept.
assert_scenario "linux-only" "" \
  "@@" "@@" "$FAKE_SHA" \
  'on_linux,linux-x86_64.tar.gz' \
  'on_macos,on_arm,darwin-arm64.tar.gz,darwin-x86_64.tar.gz'

echo
printf '== %d passed, %d failed ==\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
