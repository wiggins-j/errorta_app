# prune-formula.awk — post-process a rendered errorta Homebrew formula.
#
#   awk -v guard="depends_on arch: :arm64" -f scripts/lib/prune-formula.awk <in >out
#
# Used by scripts/release-cli.sh (and scripts/test-render-formula.sh). It:
#   1. drops any on_arm / on_intel / on_linux block whose sha256 is still a
#      @@placeholder@@ (that arch isn't built for this version),
#   2. drops an on_macos block left with no surviving leaf,
#   3. inserts the -v guard line (if any) right after the `license` line, and
#   4. collapses consecutive blank lines.
#
# Portable awk only (no gawk 3-arg match). The template fixes indentation: the
# arch leaves `on_arm`/`on_intel` sit at 4 spaces (nested under both `on_macos`
# and `on_linux`, which sit at 2), so blocks are matched by exact `do`/`end`
# strings rather than a captured indent. A leaf is pruned when its sha256 is a
# placeholder; a parent os-block is then pruned when nothing survives inside it.

{ line[NR] = $0 }

END {
  n = NR

  # 0) drop the template-doc header (everything before `class ... < Formula`).
  #    That comment describes the template, not the published formula, and it
  #    contains literal @@…@@ text that must not leak into the tap.
  cls = 0
  for (i = 1; i <= n; i++) if (line[i] ~ /^class[ \t].*<[ \t]*Formula/) { cls = i; break }
  if (cls > 1) for (i = 1; i < cls; i++) del[i] = 1

  # 1) prune arch leaves (4-space on_arm/on_intel) whose sha256 is a placeholder.
  #    Works identically whether the leaf is nested under on_macos or on_linux.
  i = 1
  while (i <= n) {
    if (line[i] == "    on_arm do" || line[i] == "    on_intel do") {
      j = i + 1; ph = 0
      while (j <= n && line[j] != "    end") {
        if (line[j] ~ /sha256 "@@/) ph = 1
        j++
      }
      if (ph) { for (k = i; k <= j; k++) del[k] = 1 }
      i = j + 1
      continue
    }
    i++
  }

  # 2) drop an on_macos / on_linux parent left with no surviving body.
  i = 1
  while (i <= n) {
    if (line[i] == "  on_macos do" || line[i] == "  on_linux do") {
      j = i + 1
      while (j <= n && line[j] != "  end") j++
      empty = 1
      for (k = i + 1; k < j; k++)
        if (!(k in del) && line[k] !~ /^[[:space:]]*$/) { empty = 0; break }
      if (empty) for (k = i; k <= j; k++) del[k] = 1
      i = j + 1
      continue
    }
    i++
  }

  # 3) emit; insert guard after license; collapse consecutive blank lines
  pb = 0
  for (i = 1; i <= n; i++) {
    if (i in del) continue
    s = line[i]
    if (s ~ /^[[:space:]]*$/) { if (pb) continue; pb = 1 } else pb = 0
    print s
    if (guard != "" && s ~ /^  license /) print "  " guard
  }
}
