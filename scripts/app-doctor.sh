#!/usr/bin/env bash
# app-doctor — is the running Errorta app built from current code?
#
# Finds the running sidecar, reads /healthz build.commit, and compares it to the
# repo HEAD. A stale bundled app (one built before the latest code landed) is the
# usual cause of "a feature 404s" or a confusing "sidecar unreachable" — this
# turns that into a one-line answer + the exact fix.
#
#   bash scripts/app-doctor.sh                 # auto-detect the running sidecar
#   bash scripts/app-doctor.sh --base http://127.0.0.1:55167
#
# Exit codes: 0 current · 1 stale · 2 no running app · 3 usage/error.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base) BASE="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 3 ;;
  esac
done

note() { printf '%s\n' "$*"; }

# --- locate the running sidecar's loopback port -----------------------------
detect_base() {
  local pids port
  pids="$(pgrep -f 'errorta-sidecar|errorta_app\.server' 2>/dev/null || true)"
  [[ -z "$pids" ]] && return 1
  for pid in $pids; do
    # the loopback (127.0.0.1) LISTEN socket is the one the webview uses
    port="$(lsof -nP -iTCP -sTCP:LISTEN -a -p "$pid" 2>/dev/null \
      | awk '/127\.0\.0\.1:/ { sub(/.*127\.0\.0\.1:/,"",$9); print $9; exit }')"
    if [[ -n "${port:-}" ]]; then
      echo "http://127.0.0.1:$port"
      return 0
    fi
  done
  return 1
}

if [[ -z "$BASE" ]]; then
  if ! BASE="$(detect_base)"; then
    note "✗ no running Errorta sidecar found."
    note "  Launch the app (or 'python -m errorta_app.server'), or pass --base http://127.0.0.1:<port>."
    exit 2
  fi
fi

HEALTH="$(curl -fsS --max-time 5 "$BASE/healthz" 2>/dev/null || true)"
if [[ -z "$HEALTH" ]]; then
  note "✗ sidecar at $BASE did not answer /healthz (it may still be starting, or the port is wrong)."
  exit 2
fi

# --- parse healthz (python: always available) -------------------------------
read -r RUN_COMMIT RUN_SHORT BUILT_AT SRC GROUNDING AIAR_SRC <<EOF
$(printf '%s' "$HEALTH" | python3 -c '
import sys, json
h = json.load(sys.stdin)
b = h.get("build") or {}
f = h.get("features") or {}
print(b.get("commit") or "none", b.get("commit_short") or "none",
      (b.get("built_at") or "?").replace(" ", "_"), b.get("source") or "?",
      "yes" if f.get("grounding") else "no",
      (h.get("aiar_pin") or {}).get("source") or "?")
')
EOF

HEAD="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
HEAD_SHORT="${HEAD:0:12}"

note "Errorta app doctor"
note "  sidecar:       $BASE"
note "  app built from: $RUN_SHORT  (source=$SRC, built_at=$BUILT_AT)"
note "  repo HEAD:      $HEAD_SHORT"
note "  grounding:     $GROUNDING        aiar: $AIAR_SRC"

if [[ "$RUN_COMMIT" == "none" ]]; then
  note ""
  note "✗ STALE — this app predates build provenance (no commit stamp), so it is"
  note "  older than the build-freshness infrastructure. Rebuild:"
  note "      bash scripts/rebuild-app.sh --install"
  exit 1
fi

if [[ "$RUN_COMMIT" == "$HEAD" ]]; then
  note ""
  note "✓ CURRENT — the running app matches repo HEAD."
  exit 0
fi

# how far behind, if the running commit is known locally
BEHIND=""
if git -C "$REPO_ROOT" cat-file -e "$RUN_COMMIT^{commit}" 2>/dev/null; then
  BEHIND="$(git -C "$REPO_ROOT" rev-list --count "$RUN_COMMIT..HEAD" 2>/dev/null || true)"
fi
note ""
note "✗ STALE — the running app is NOT built from current code${BEHIND:+ ($BEHIND commits behind)}."
note "  Symptoms of this drift: new routes 404, features silently missing,"
note "  or a misleading 'sidecar unreachable' banner."
note "  Fix:  bash scripts/rebuild-app.sh --install"
exit 1
