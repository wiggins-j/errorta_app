#!/bin/zsh
# SPEC-40 DoD: re-probe the three DELIVERED gravity-golf trees (not fixtures) and
# report the verdict components the gate hierarchy consumes.
#   golf-2 must BLOCK (genuinely inert -> confident inert, path 3)
#   golf-4 must UNBLOCK (the mechanic is live once the sweep is calibrated)
#   golf-3 must stay GREEN (SPEC-38's interaction fix not regressed)
set -u
WS="$HOME/.errorta/council/apply-workspaces"
# Derive the repo root from this script's own location rather than hardcoding a
# maintainer's absolute path: the literal baked in a local account name into a
# PUBLIC repo, and the script only ran on one machine.
REPO="${0:a:h:h}"
export PLAYWRIGHT_BROWSERS_PATH="$HOME/Library/Caches/ms-playwright"

for name in gravity-golf-2 gravity-golf-3 gravity-golf-4; do
  dir="$WS/coding-$name"
  if [[ ! -d "$dir" ]]; then echo "$name: MISSING"; continue; fi
  port=$((19000 + RANDOM % 2000))
  (cd "$dir" && python3 -m http.server $port >/dev/null 2>&1) &
  srv=$!
  sleep 1.2
  for mode in adaptive legacy; do
    if [[ $mode == legacy ]]; then flags=(--legacy-sweep); else flags=(); fi
    out=$(cd "$REPO" && node scripts/web-probe.mjs "http://127.0.0.1:$port/" 30 "${flags[@]}" 2>/dev/null | tail -1)
    echo "$out" | python3 -c "
import sys, json
raw = sys.stdin.read().strip()
if not raw:
    print('  $name/$mode: NO VERDICT'); raise SystemExit
d = json.loads(raw)
mp = d.get('mechanic_probe') or {}
wb = d.get('whitebox') or {}
print('  $name/$mode: interaction=%s ran=%s matters=%s confident=%s p_sink=%s max_gap=%s whitebox=%s' % (
    d.get('interaction_changed'), mp.get('ran'), mp.get('mechanic_matters'),
    mp.get('confident'), mp.get('p_sink'), mp.get('max_gap'),
    wb.get('verdict') if wb.get('has_contract') else 'no-contract'))
if not mp.get('ran'): print('      reason:', (mp.get('reason') or '')[:150])
"
  done
  kill $srv 2>/dev/null
done
