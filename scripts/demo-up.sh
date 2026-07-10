#!/usr/bin/env bash
# demo-up.sh — bring up the F031 Council demo (sidecar + Vite frontend)
#
# Boots the Python sidecar under uvicorn, polls /healthz until ready
# AND validates the AIAR readiness contract (QA P1 #2, 2026-06-12),
# then boots the Vite dev server. Traps INT/TERM/EXIT and kills both
# child PIDs so Ctrl-C leaves no orphans.
#
# This is a dev helper, NOT a deployment script. Per PM resolution
# (2026-06-12), no --seed flag — the operator clicking the in-app
# "Seed demo room" button IS the demo moment.
#
# NOTE: --tauri was REMOVED 2026-06-12 (QA P1 #4). The Tauri lifespan
# spawns its own sidecar on a Tauri-allocated port and the UI prefers
# that port; demo-up.sh starting a second sidecar on ERRORTA_SIDECAR_PORT
# was healthz-polling a sidecar the UI never spoke to. For the Tauri
# dev path, run `npm run tauri:dev` directly (Tauri owns both processes).
#
# Plan: docs/superpowers/plans/2026-06-12-F031-DEMO-BOOT-VERIFY.md
# Spec: docs/specs/F031-DEMO-BOOT-VERIFY-boot-sequence.md

set -euo pipefail

# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------

_usage() {
    cat <<'EOF'
Usage: scripts/demo-up.sh [flags]

Brings up the Errorta sidecar (python -m errorta_app.server) and the
Vite dev server and prints a ready banner.

Flags:
  --check-corpus    Before booting the frontend, HEAD the welcome-corpus
                    release URL pinned in python/errorta_welcome/
                    pinned_hash.json. Exits non-zero on a non-200 (or
                    on a draft-placeholder URL) so the operator knows
                    to pre-stage the local tarball. Skipped under
                    --offline / DEMO_OFFLINE=1.
  --offline         Same effect as DEMO_OFFLINE=1. Skips the corpus
                    reachability check.
  -h, --help        Show this message and exit 0.

Environment:
  ERRORTA_SIDECAR_PORT  Sidecar bind port. Default 8770.
  DEMO_OFFLINE          Boolean (1/true). Skip corpus reachability.

Logs:
  Sidecar  -> .errorta-demo-logs/sidecar.log
  Frontend -> .errorta-demo-logs/frontend.log

Ctrl-C terminates both children and waits for them to exit.

For the Tauri dev path, run `npm run tauri:dev` directly — Tauri spawns
its own sidecar and the UI uses the Tauri-allocated port.

EOF
}

# ---------------------------------------------------------------------------
# flag parsing
# ---------------------------------------------------------------------------

CHECK_CORPUS=0
OFFLINE="${DEMO_OFFLINE:-0}"
[[ "${OFFLINE}" == "true" ]] && OFFLINE=1

# QA P1 #4 (2026-06-12): --tauri removed. If we see it (or DEMO_TAURI=1),
# fail loudly with the migration pointer so an operator's muscle memory
# doesn't silently launch a misconfigured demo.
if [[ "${DEMO_TAURI:-0}" == "1" || "${DEMO_TAURI:-0}" == "true" ]]; then
    echo "DEMO_TAURI is no longer supported by demo-up.sh (QA P1 #4)." >&2
    echo "For the Tauri dev path, run: npm run tauri:dev" >&2
    echo "(Tauri spawns its own sidecar; the UI uses the Tauri port.)" >&2
    exit 2
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tauri)
            echo "--tauri is no longer supported (QA P1 #4)." >&2
            echo "For the Tauri dev path, run: npm run tauri:dev" >&2
            echo "(Tauri spawns its own sidecar; the UI uses the Tauri port.)" >&2
            exit 2
            ;;
        --check-corpus)
            CHECK_CORPUS=1
            shift
            ;;
        --offline)
            OFFLINE=1
            shift
            ;;
        -h|--help)
            _usage
            exit 0
            ;;
        *)
            echo "unknown flag: $1" >&2
            _usage >&2
            exit 2
            ;;
    esac
done

# ---------------------------------------------------------------------------
# env defaults
# ---------------------------------------------------------------------------

: "${ERRORTA_SIDECAR_PORT:=8770}"
export ERRORTA_SIDECAR_PORT

VITE_URL="http://127.0.0.1:1420"

# ---------------------------------------------------------------------------
# pre-flight
# ---------------------------------------------------------------------------

# Prefer `python3` (default on modern macOS / Linux distros); fall back
# to `python` if only that is installed.
PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    echo "missing required command: python3 (or python)" >&2
    echo "install Python 3.10+ and re-run scripts/demo-up.sh" >&2
    exit 3
fi

for cmd in node npm curl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "missing required command: $cmd" >&2
        echo "install it and re-run scripts/demo-up.sh" >&2
        exit 3
    fi
done

mkdir -p .errorta-demo-logs
SIDECAR_LOG=".errorta-demo-logs/sidecar.log"
FRONTEND_LOG=".errorta-demo-logs/frontend.log"

# ---------------------------------------------------------------------------
# cleanup trap (idempotent)
# ---------------------------------------------------------------------------

SIDECAR_PID=""
FRONTEND_PID=""

_cleanup() {
    # Clear the trap first so re-entry (EXIT after INT) is a no-op.
    trap - INT TERM EXIT
    if [[ -n "$FRONTEND_PID" ]]; then
        kill -TERM "$FRONTEND_PID" 2>/dev/null || true
    fi
    if [[ -n "$SIDECAR_PID" ]]; then
        kill -TERM "$SIDECAR_PID" 2>/dev/null || true
    fi
    wait 2>/dev/null || true
}
trap _cleanup INT TERM EXIT

# ---------------------------------------------------------------------------
# welcome-corpus reachability
# ---------------------------------------------------------------------------
# HEAD the F-INFRA-11 release URL pinned in python/errorta_welcome/
# pinned_hash.json. Skipped under --offline / DEMO_OFFLINE=1.
#
# Draft detection (implementer-call per plan §PM resolutions #4):
# - empty / missing source_url field -> treated as draft
# - non-200 HEAD on the latest/download/ URL -> treated as draft
#   (as of 2026-06-12 this is the de-facto signal — F-INFRA-11
#   slice (e) un-drafts the release on wiggins-j/errorta-downloads).
#
# Exits non-zero on both draft paths so the operator knows to either
# pre-stage the tarball locally or re-run with --offline.

_check_corpus_reachability() {
    if [[ "$OFFLINE" == "1" ]]; then
        echo "corpus check skipped (DEMO_OFFLINE=1)"
        return 0
    fi

    local url
    url=$("$PYTHON_BIN" -c '
import json, pathlib, sys
p = pathlib.Path("python/errorta_welcome/pinned_hash.json")
if not p.is_file():
    sys.exit(0)
try:
    d = json.loads(p.read_text())
except Exception:
    sys.exit(0)
print(d.get("source_url") or d.get("url") or "")
' 2>/dev/null)

    if [[ -z "$url" ]]; then
        echo "welcome-corpus release URL is empty (draft marker)." >&2
        echo "pre-stage at: python/errorta_welcome/welcome-corpus.tar.gz" >&2
        echo "or re-run with --offline / DEMO_OFFLINE=1" >&2
        return 5
    fi

    if curl -fsSI --max-time 5 -o /dev/null "$url"; then
        echo "corpus reachable: $url"
        return 0
    fi

    echo "welcome-corpus HEAD failed for $url" >&2
    echo "the F-INFRA-11 release is likely still draft" >&2
    echo "" >&2
    echo "pre-stage at: python/errorta_welcome/welcome-corpus.tar.gz" >&2
    echo "or re-run with --offline / DEMO_OFFLINE=1" >&2
    return 5
}

# ---------------------------------------------------------------------------
# boot sidecar
# ---------------------------------------------------------------------------

echo "starting sidecar on 127.0.0.1:${ERRORTA_SIDECAR_PORT}..."
"$PYTHON_BIN" -m errorta_app.server >"$SIDECAR_LOG" 2>&1 &
SIDECAR_PID=$!

# QA P1 #2 (2026-06-12): the previous grep -q '"available"' was too loose
# — the substring matches `"available": false` as well, so the script would
# report READY for a sidecar with no AIAR install at all (and the demo
# would land users on an "AIAR not available" banner anyway). The strict
# gate parses JSON and demands the three fields the runbook promises:
#     council             == true
#     aiar_pin.available  == true
#     aiar_pin.source     in {"editable", "pinned"}
#
# (source=="absent" is the only "not OK" value — the smoke test in
#  python/tests/test_sidecar_boot_smoke.py asserts the looser shape
#  contract; here we are gating the demo path specifically.)

_healthz_ready_strict() {
    # Pipe the JSON to Python on stdin (works on bash 3.2 — no ${var@Q}).
    local body
    body=$(curl -fsS "http://127.0.0.1:${ERRORTA_SIDECAR_PORT}/healthz" 2>/dev/null) || return 1
    printf '%s' "$body" | "$PYTHON_BIN" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(2)
if d.get("council") is not True:
    sys.exit(3)
pin = d.get("aiar_pin") or {}
if pin.get("available") is not True:
    sys.exit(4)
if pin.get("source") not in ("editable", "pinned"):
    sys.exit(5)
' 2>/dev/null
}

# poll /healthz with a 30s wall budget, 250ms interval.
deadline=$((SECONDS + 30))
while true; do
    if _healthz_ready_strict; then
        break
    fi
    if ! kill -0 "$SIDECAR_PID" 2>/dev/null; then
        echo "sidecar process exited before healthz returned" >&2
        echo "--- tail of $SIDECAR_LOG ---" >&2
        tail -n 40 "$SIDECAR_LOG" >&2 || true
        echo "hint: if uvicorn is missing, run: pip install uvicorn" >&2
        exit 4
    fi
    if (( SECONDS >= deadline )); then
        echo "sidecar healthz did not pass the strict demo gate within 30s" >&2
        echo "(required: council==true, aiar_pin.available==true," >&2
        echo " aiar_pin.source in {editable,pinned})" >&2
        echo "--- last healthz body ---" >&2
        curl -sS "http://127.0.0.1:${ERRORTA_SIDECAR_PORT}/healthz" >&2 || true
        echo >&2
        echo "--- tail of $SIDECAR_LOG ---" >&2
        tail -n 40 "$SIDECAR_LOG" >&2 || true
        echo "hint: if AIAR is missing, run: pip install -e ../aiar" >&2
        exit 4
    fi
    sleep 0.25
done

# ---------------------------------------------------------------------------
# optional corpus check (Task 3 fills in the body)
# ---------------------------------------------------------------------------

if (( CHECK_CORPUS )); then
    _check_corpus_reachability
fi

# ---------------------------------------------------------------------------
# boot frontend
# ---------------------------------------------------------------------------

echo "starting frontend via 'npm run dev'..."
npm run dev >"$FRONTEND_LOG" 2>&1 &
FRONTEND_PID=$!

# Give the frontend a moment to bind, then verify it didn't die immediately
# (the common failure mode is Vite refusing to bind to 127.0.0.1:1420
# because another instance already holds the port). If it dies, dump the
# log tail so EADDRINUSE shows up without making the operator hunt.
sleep 1
if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    echo "frontend process exited immediately after spawn" >&2
    echo "--- tail of $FRONTEND_LOG ---" >&2
    tail -n 40 "$FRONTEND_LOG" >&2 || true
    echo "hint: another Vite instance may already hold ${VITE_URL}" >&2
    exit 6
fi

# ---------------------------------------------------------------------------
# ready banner + wait
# ---------------------------------------------------------------------------

echo "READY  sidecar :${ERRORTA_SIDECAR_PORT}  |  frontend ${VITE_URL}"

# wait on the frontend so Ctrl-C reaches the trap.
wait "$FRONTEND_PID"
