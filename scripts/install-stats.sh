#!/usr/bin/env bash
# install-stats.sh — errorta CLI adoption, via GitHub release download counts.
#
# There is NO official Homebrew analytics for a third-party tap (formulae.brew.sh
# only tracks homebrew/core + cask). But `brew install`/`brew upgrade` fetch the
# release tarball with curl, so each release asset's `download_count` is the best
# available install proxy: a fresh install/upgrade downloads it once; a cached
# install does not. (Tap *clone* traffic is NOT used — it's polluted by
# `brew update` fetches and Homebrew's own bots.)
#
# Usage:
#   scripts/install-stats.sh            # all CLI releases
#   scripts/install-stats.sh --json     # raw JSON (for scripting)
#   REPO=owner/name scripts/install-stats.sh
#
# Requires: gh (authenticated), jq.
set -euo pipefail

REPO="${REPO:-wiggins-j/errorta_app}"
JSON=0
[ "${1:-}" = "--json" ] && JSON=1

command -v gh >/dev/null 2>&1 || { echo "error: gh CLI not found (brew install gh)" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "error: jq not found (brew install jq)" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "error: gh not authenticated (gh auth login)" >&2; exit 1; }

# Pull every release + its assets. Filter to CLI releases (tag prefix cli-v),
# which is where the Homebrew tarballs live.
releases="$(gh api "repos/${REPO}/releases" --paginate --slurp \
  | jq '[ .[][] | select(.tag_name | startswith("cli-v"))
          | { tag: .tag_name, prerelease: .prerelease, published: .published_at,
              assets: [ .assets[] | { name: .name, downloads: .download_count } ] } ]')"

if [ "$JSON" = "1" ]; then
  echo "$releases"
  exit 0
fi

if [ "$(jq 'length' <<<"$releases")" = "0" ]; then
  echo "No cli-v* releases found in ${REPO}."
  exit 0
fi

echo "errorta CLI installs (GitHub release downloads — install proxy) — ${REPO}"
echo
jq -r '
  .[] |
  "\(.tag)\(if .prerelease then "  (prerelease)" else "" end)",
  ( .assets[] | (.downloads | tostring) as $d
      | "    \((6 - ($d | length)) as $pad | (if $pad > 0 then " " * $pad else "" end) + $d)  \(.name)" ),
  ( if (.assets | length) == 0 then "    (no assets)" else empty end ),
  ""
' <<<"$releases"

total="$(jq '[ .[].assets[].downloads ] | add // 0' <<<"$releases")"
printf 'Total downloads across all CLI release assets: %s\n' "$total"
echo
echo "Note: this counts binary fetches (fresh installs + upgrades), not unique users;"
echo "cached re-installs don't re-download. No per-user Homebrew analytics exists for a tap."
