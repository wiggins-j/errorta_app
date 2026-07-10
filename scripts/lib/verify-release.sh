#!/usr/bin/env bash
# F-DIST-02 — post-build self-verification, sourced by release-macos.sh. Also
# runnable standalone to check a produced app+dmg:
#
#   source scripts/lib/verify-release.sh
#   verify_release <app-path> <dmg-path> <expected_gate:0|1>
#
# Asserts: the app is Gatekeeper-accepted as "Notarized Developer ID"; the app
# AND dmg are stapled; and the enclosed sidecar actually boots (catching a
# hardened-runtime / library-validation regression BEFORE distribution) with a
# gate stamp matching what was requested. Returns non-zero on any failure.

verify_release() {
  local app="${1:?verify_release <app> <dmg> <expected_gate>}"
  local dmg="${2:?verify_release <app> <dmg> <expected_gate>}"
  local expected_gate="${3:?verify_release <app> <dmg> <expected_gate>}"
  local fail=0

  echo "[verify] Gatekeeper assessment on the app ..."
  if spctl --assess --type execute --verbose=4 "$app" 2>&1 | grep -q "source=Notarized Developer ID"; then
    echo "[verify]   OK: Notarized Developer ID"
  else
    echo "[verify]   FAIL: app not Gatekeeper-accepted as Notarized Developer ID" >&2; fail=1
  fi

  echo "[verify] stapler validate (app + dmg) ..."
  if xcrun stapler validate "$app" >/dev/null 2>&1; then echo "[verify]   OK: app stapled"; else echo "[verify]   FAIL: app not stapled" >&2; fail=1; fi
  if xcrun stapler validate "$dmg" >/dev/null 2>&1; then echo "[verify]   OK: dmg stapled"; else echo "[verify]   FAIL: dmg not stapled" >&2; fail=1; fi

  echo "[verify] booting the enclosed sidecar (expect gate=$expected_gate) ..."
  local sc="$app/Contents/MacOS/errorta-sidecar"
  local port=8899 home ok=0 pid
  home="$(mktemp -d)"
  ERRORTA_HOME="$home" ERRORTA_SIDECAR_PORT="$port" "$sc" >"$home/sidecar.log" 2>&1 &
  pid=$!
  for _ in $(seq 1 40); do
    if curl -fsS "http://127.0.0.1:$port/healthz" -o "$home/hz.json" 2>/dev/null; then ok=1; break; fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if [[ $ok -eq 1 ]]; then
    local stamped
    stamped="$(python3 -c "import json;print('1' if json.load(open('$home/hz.json'))['build']['alpha_gate_enabled'] else '0')" 2>/dev/null || echo '?')"
    if [[ "$stamped" == "$expected_gate" ]]; then
      echo "[verify]   OK: sidecar boots; alpha_gate_enabled matches ($stamped)"
    else
      echo "[verify]   FAIL: gate stamp=$stamped, expected $expected_gate" >&2; fail=1
    fi
    if [[ "$expected_gate" == "1" ]]; then
      if curl -fsS "http://127.0.0.1:$port/alpha/status" >/dev/null 2>&1; then
        echo "[verify]   OK: /alpha/status responds under the gate"
      else
        echo "[verify]   FAIL: /alpha/status not responding under the gate" >&2; fail=1
      fi
    fi
  else
    echo "[verify]   FAIL: sidecar did not boot (hardened-runtime / library-validation?) — see log:" >&2
    tail -15 "$home/sidecar.log" >&2 || true
    fail=1
  fi
  kill "$pid" 2>/dev/null || true; sleep 1; kill -9 "$pid" 2>/dev/null || true
  rm -rf "$home"

  if [[ $fail -ne 0 ]]; then
    echo "[verify] RELEASE VERIFICATION FAILED" >&2
    return 1
  fi
  echo "[verify] all checks passed"
}
