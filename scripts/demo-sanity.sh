#!/usr/bin/env bash
# demo-sanity.sh — pre-demo sanity check.
#
# Run this 5 minutes before the audience walks in. Asserts every
# precondition the F031 Council demo depends on, then drives one full
# round through the running sidecar's API surface to prove the
# end-to-end path is hot. Tears down the probe room on exit so the
# real demo starts from a clean Rooms list.
#
# Exits non-zero on the first failed check with a clear pointer to
# what to fix and where. Total wall time on a warm machine: ~20s.
#
# This script does NOT launch the Mac app — it assumes either
# /Applications/Errorta.app is already running OR you're using the
# two-process dev path (`scripts/demo-up.sh`) on a separate terminal.
# It auto-detects the running sidecar by scanning lsof.
#
# Usage:
#   bash scripts/demo-sanity.sh
#   bash scripts/demo-sanity.sh --port 8770    # override auto-detect
#   bash scripts/demo-sanity.sh --keep-room    # don't tear down probe room
#
# See: docs/demos/F031-demo-test-plan.md
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT=""
KEEP_ROOM=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --keep-room) KEEP_ROOM=1; shift ;;
        -h|--help) sed -n '1,30p' "$0"; exit 0 ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
    esac
done

# Pretty output
red()   { printf "\033[31m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
bold()  { printf "\033[1m%s\033[0m\n" "$*"; }

fail() { red "FAIL  $1"; [[ $# -gt 1 ]] && echo "       → $2"; exit 1; }
pass() { green "PASS  $1"; }
step() { bold "$1"; }

# ---------------------------------------------------------------------------
# 1. Filesystem preconditions
# ---------------------------------------------------------------------------

step "[1/8] Filesystem preconditions"
[[ -f "$REPO_ROOT/dist/welcome-corpus.tar.gz" ]] \
    || fail "welcome-corpus.tar.gz missing" \
            "bash scripts/build-welcome-corpus.sh"
pass "dist/welcome-corpus.tar.gz present ($(wc -c < "$REPO_ROOT/dist/welcome-corpus.tar.gz") bytes)"

[[ -d "$REPO_ROOT/python/.venv" ]] \
    || fail "python/.venv missing" \
            "python3 -m venv python/.venv && python/.venv/bin/pip install -e 'python/.[dev]' && python/.venv/bin/pip install -e ../aiar"
pass "python/.venv exists"

"$REPO_ROOT/python/.venv/bin/python" -c "import aiar" 2>/dev/null \
    || fail "AIAR not importable from python/.venv" \
            "python/.venv/bin/pip install -e ../aiar"
pass "AIAR importable from venv"

# ---------------------------------------------------------------------------
# 2. Locate the running sidecar
# ---------------------------------------------------------------------------

step "[2/8] Sidecar discovery"
if [[ -z "$PORT" ]]; then
    PORT=$(lsof -nP -iTCP -sTCP:LISTEN -a -c errorta-sidecar 2>/dev/null \
        | awk '/LISTEN/ {n=split($9,a,":"); print a[n]; exit}')
fi
[[ -n "$PORT" ]] \
    || fail "no errorta-sidecar listening" \
            "open /Applications/Errorta.app  OR  bash scripts/demo-up.sh"
pass "sidecar listening on 127.0.0.1:$PORT"

# ---------------------------------------------------------------------------
# 3. /healthz strict gate
# ---------------------------------------------------------------------------

step "[3/8] /healthz strict gate"
HEALTHZ=$(curl -fsS "http://127.0.0.1:$PORT/healthz" 2>/dev/null) \
    || fail "/healthz did not return 200" "check the sidecar log"

echo "$HEALTHZ" | python3 -c '
import json, sys
d = json.load(sys.stdin)
if d.get("council") is not True:
    print("council!=true", file=sys.stderr); sys.exit(3)
pin = d.get("aiar_pin") or {}
if pin.get("available") is not True:
    print("aiar_pin.available!=true (source=" + str(pin.get("source")) + ")", file=sys.stderr); sys.exit(4)
if pin.get("source") not in ("editable", "pinned"):
    print("aiar_pin.source=" + str(pin.get("source")), file=sys.stderr); sys.exit(5)
' || fail "healthz strict gate failed" "see stderr above"
SOURCE=$(echo "$HEALTHZ" | python3 -c 'import json,sys; print(json.load(sys.stdin)["aiar_pin"]["source"])')
pass "council=true, aiar_pin.available=true, aiar_pin.source=$SOURCE"

# ---------------------------------------------------------------------------
# 4. Welcome corpus ingested (idempotent)
# ---------------------------------------------------------------------------

step "[4/8] Welcome corpus ingest"
INGEST=$(curl -fsS -X POST "http://127.0.0.1:$PORT/welcome/ingest" \
    -H 'Content-Type: application/json' \
    -d "{\"tarball_path\": \"$REPO_ROOT/dist/welcome-corpus.tar.gz\"}" 2>/dev/null) \
    || fail "/welcome/ingest failed" "see sidecar log"
FILES=$(echo "$INGEST" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("files_ingested",0))')
F004=$(echo "$INGEST" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("f004_invoked",False))')
[[ "$FILES" -ge 5 ]] || fail "expected >=5 files ingested, got $FILES"
[[ "$F004" == "True" ]] || fail "f004_invoked!=true ($F004)"
pass "welcome corpus ingested ($FILES files, f004_invoked=$F004)"

# ---------------------------------------------------------------------------
# 5. Probe Council room (mirrors src/features/council/CouncilDemoRoomSeed.ts
#    so the probe exercises the same schema path the live UI does)
# ---------------------------------------------------------------------------

step "[5/8] Council probe room"
ROOM_ID="demo-sanity-$(date +%s)"
ROOM_JSON=$(cat <<JSON
{
  "format_version": 1,
  "id": "$ROOM_ID",
  "name": "Demo Sanity Probe",
  "description": "Created by scripts/demo-sanity.sh — auto-deleted on success.",
  "preset_id": null,
  "status_hint": "draft",
  "members": [
    {
      "id": "m-1", "name": "Member 1 (full)", "role": "answerer", "enabled": true,
      "gateway_route_id": "fake.local.deterministic",
      "provider_kind": "local", "provider_display": "Fake", "model_display": "deterministic",
      "catalog_version": "2026-06-12",
      "context_access": "full_context", "transcript_access": "own_messages",
      "turn_limits": {"max_messages": 1, "max_input_tokens": 1024,
                      "max_output_tokens": 256, "max_context_tokens": 1024},
      "generation": {"temperature": 0.0, "top_p": null, "seed": null},
      "system_prompt": "demo-sanity probe", "metadata": {}
    },
    {
      "id": "m-2", "name": "Member 2 (redacted)", "role": "answerer", "enabled": true,
      "gateway_route_id": "fake.local.deterministic",
      "provider_kind": "local", "provider_display": "Fake", "model_display": "deterministic",
      "catalog_version": "2026-06-12",
      "context_access": "redacted_summary", "transcript_access": "own_messages",
      "turn_limits": {"max_messages": 1, "max_input_tokens": 1024,
                      "max_output_tokens": 256, "max_context_tokens": 1024},
      "generation": {"temperature": 0.0, "top_p": null, "seed": null},
      "system_prompt": "demo-sanity probe", "metadata": {}
    }
  ],
  "topology": {
    "kind": "round_robin", "max_rounds": 1, "max_messages_per_member": 1,
    "max_total_turns": 2, "speaker_order": ["m-1", "m-2"], "stop_condition": null
  },
  "context_policy": {
    "default_context_access": "prompt_only",
    "default_transcript_access": "own_messages",
    "allow_full_context": true,
    "require_confirmation_for_remote_context": true,
    "require_confirmation_for_full_context": false
  },
  "budget_policy": {
    "max_rounds": 1, "max_messages_per_member": 1, "max_total_model_calls": 2,
    "max_remote_calls_per_run": 0, "max_remote_calls_per_day": null,
    "max_input_tokens_per_turn": 1024, "max_output_tokens_per_turn": 256,
    "max_context_tokens_per_member": 1024,
    "max_estimated_usd_per_run": 0.0, "max_estimated_usd_per_month": null
  },
  "finalization_policy": {
    "mode": "transcript_only", "finalizer_member_id": null,
    "judge_member_ids": [], "require_judge_verdict": false,
    "allow_minority_report": true, "allow_grounding_write": false,
    "grounding_requires_user_accept": true
  },
  "ui": {},
  "created_at": "2026-06-12T00:00:00Z",
  "updated_at": "2026-06-12T00:00:00Z",
  "last_validated_at": null,
  "revision": 1,
  "corpus_ids": ["welcome"],
  "metadata": {"demo_marker": "demo-sanity"}
}
JSON
)
CREATE_RESP=$(curl -fsS -X POST "http://127.0.0.1:$PORT/council/rooms" \
    -H 'Content-Type: application/json' -d "$ROOM_JSON" 2>/dev/null) \
    || fail "POST /council/rooms failed (status non-2xx)" "see sidecar log for schema errors"
pass "room created: $ROOM_ID"

# ---------------------------------------------------------------------------
# 6. Run the probe room (engine-backed — mirrors the live demo path
#    so context manifests get written and /inspection has something
#    to return; the dry_fake_members path skips manifest writes)
# ---------------------------------------------------------------------------

step "[6/8] Run probe room (engine-backed)"
RUN_BODY="{\"room_id\":\"$ROOM_ID\",\"prompt\":\"demo sanity probe\",\"corpus_ids\":[\"welcome\"]}"
RUN_RESP=$(curl -fsS -X POST "http://127.0.0.1:$PORT/council/runs" \
    -H 'Content-Type: application/json' -d "$RUN_BODY" 2>/dev/null) \
    || fail "POST /council/runs failed" "see sidecar log"
RUN_ID=$(echo "$RUN_RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("run",{}).get("id") or d.get("run_id",""))')
[[ -n "$RUN_ID" ]] || fail "run id missing in response" "$RUN_RESP"
# Poll until terminal — fake.local.deterministic routes finish in <1s
# but allow up to 10s in case the machine is hot.
STATE="?"
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    STATE=$(curl -fsS "http://127.0.0.1:$PORT/council/runs/$RUN_ID" 2>/dev/null \
        | python3 -c '
import json,sys
d = json.load(sys.stdin)
run = d.get("run") or d
print(run.get("status") or run.get("state") or "?")
')
    case "$STATE" in
        succeeded|completed|finished|failed|cancelled) break ;;
    esac
    sleep 0.5
done
case "$STATE" in
    succeeded|completed|finished) ;;
    *) fail "run did not reach terminal succeeded state (got $STATE)" \
            "see sidecar log for engine error" ;;
esac
pass "run $RUN_ID reached state=$STATE"

# ---------------------------------------------------------------------------
# 7. Round-level /inspection (marquee: m-1 and m-2 get different bytes)
# ---------------------------------------------------------------------------

step "[7/8] Round-level /inspection"
INSP=$(curl -fsS "http://127.0.0.1:$PORT/council/runs/$RUN_ID/rounds/1/inspection" 2>/dev/null) \
    || fail "/inspection 404 on round 1" "engine wrote no manifest — check sidecar log"
MC=$(echo "$INSP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("manifest_count",0))')
[[ "$MC" -ge 2 ]] || fail "manifest_count=$MC (want >=2; one per member)"
# Confirm the two members got distinct payload hashes — the marquee invariant.
DISTINCT=$(echo "$INSP" | python3 -c '
import json,sys
d = json.load(sys.stdin)
hashes = sorted({m.get("payload_sha256","") for m in d.get("manifests",[]) if m.get("payload_sha256")})
print(len(hashes))
')
[[ "$DISTINCT" -ge 2 ]] || fail "members share a payload hash ($DISTINCT distinct; want >=2)" \
                                "byte-isolation regression — DO NOT DEMO"
pass "manifest_count=$MC, distinct payload hashes=$DISTINCT  (marquee invariant holds)"

# ---------------------------------------------------------------------------
# 8. Teardown
# ---------------------------------------------------------------------------

step "[8/8] Teardown"
if [[ "$KEEP_ROOM" == "1" ]]; then
    echo "      probe room kept: $ROOM_ID"
else
    curl -fsS -X DELETE "http://127.0.0.1:$PORT/council/rooms/$ROOM_ID" >/dev/null 2>&1 \
        && pass "probe room deleted" \
        || echo "      (room delete returned non-zero; harmless — operator should remove manually if it shows)"
fi

bold ""
green "===================================================================="
green "  DEMO READY  ·  sidecar :$PORT  ·  aiar_pin.source=$SOURCE"
green "===================================================================="
