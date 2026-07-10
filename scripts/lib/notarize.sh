#!/usr/bin/env bash
# F-DIST-02 — notarization helpers, sourced by release scripts.
#
#   source scripts/lib/notarize.sh
#   notarize_app <app-path>          # zips, submits, staples the .app
#   notarize_and_staple <dmg|pkg>    # submits + staples a directly-submittable artifact
#
# Credential detection (in order):
#   1. keychain profile `errorta-notary`, detected by a LIVENESS PROBE
#      (`xcrun notarytool history --keychain-profile errorta-notary`), NOT by a
#      brittle `security find-generic-password` service-name query — that query
#      reports MISSING even when the profile exists and works, the bug that made
#      the old release-macos.sh fall through to env vars and fail.
#   2. env vars APPLE_ID / APPLE_TEAM_ID / APPLE_APP_SPECIFIC_PASSWORD.
#   3. neither -> callers fail with a pointer to docs/SIGNING_MACOS.md.
#
# Requires: xcrun (Xcode Command Line Tools).

ERRORTA_NOTARY_PROFILE="${ERRORTA_NOTARY_PROFILE:-errorta-notary}"

# _notary_creds_mode: echoes "profile" | "envvars" | "" (none available)
_notary_creds_mode() {
  if xcrun notarytool history --keychain-profile "$ERRORTA_NOTARY_PROFILE" >/dev/null 2>&1; then
    echo "profile"; return 0
  fi
  if [[ -n "${APPLE_ID:-}" && -n "${APPLE_TEAM_ID:-}" && -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" ]]; then
    echo "envvars"; return 0
  fi
  echo ""
}

# _notary_submit <artifact>: submit + --wait; returns 0 iff status == Accepted.
# On non-Accepted, dumps the notary log to stderr.
_notary_submit() {
  local artifact="$1" mode out id status
  mode="$(_notary_creds_mode)"
  if [[ -z "$mode" ]]; then
    echo "[notarize] no notarization credentials." >&2
    echo "[notarize] set up the keychain profile '$ERRORTA_NOTARY_PROFILE' (xcrun notarytool store-credentials) or export APPLE_ID / APPLE_TEAM_ID / APPLE_APP_SPECIFIC_PASSWORD — see docs/SIGNING_MACOS.md." >&2
    return 1
  fi
  echo "[notarize] submitting $(basename "$artifact") (creds: $mode) ..." >&2
  if [[ "$mode" == "profile" ]]; then
    out="$(xcrun notarytool submit "$artifact" --keychain-profile "$ERRORTA_NOTARY_PROFILE" --wait 2>&1)"
  else
    out="$(xcrun notarytool submit "$artifact" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_SPECIFIC_PASSWORD" --wait 2>&1)"
  fi
  echo "$out" >&2
  # Anchor to the final "  id:" / "  status:" summary lines. Do NOT use a bare
  # /status:/ — the streaming "Current status: In Progress..." line also contains
  # "status:", and matching it (then exiting) mis-reads an Accepted job as failed.
  id="$(printf '%s\n' "$out" | awk '/^[[:space:]]*id:/{print $2; exit}')"
  status="$(printf '%s\n' "$out" | awk -F': ' '/^[[:space:]]*status:/{print $2; exit}' | tr -d '[:space:]')"
  if [[ "$status" != "Accepted" ]]; then
    echo "[notarize] status='$status' (not Accepted)." >&2
    if [[ -n "$id" ]]; then
      echo "[notarize] --- notarytool log $id ---" >&2
      if [[ "$mode" == "profile" ]]; then
        xcrun notarytool log "$id" --keychain-profile "$ERRORTA_NOTARY_PROFILE" >&2 2>/dev/null || true
      else
        xcrun notarytool log "$id" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_SPECIFIC_PASSWORD" >&2 2>/dev/null || true
      fi
    fi
    return 1
  fi
  return 0
}

# notarize_and_staple <artifact>: for a .dmg or .pkg (directly submittable AND
# stapleable). submit -> wait(Accepted) -> staple -> validate.
notarize_and_staple() {
  local artifact="${1:?usage: notarize_and_staple <dmg|pkg>}"
  _notary_submit "$artifact" || return 1
  echo "[notarize] stapling $(basename "$artifact") ..." >&2
  xcrun stapler staple "$artifact"
  xcrun stapler validate "$artifact"
}

# notarize_app <app-path>: a bare .app cannot be submitted to notarytool, so we
# zip it for SUBMISSION but staple the .app itself (a zip cannot be stapled).
# submit(zip) -> wait(Accepted) -> staple(app) -> validate(app).
notarize_app() {
  local app="${1:?usage: notarize_app <app-path>}"
  local tmp zip
  tmp="$(mktemp -d)"
  zip="$tmp/$(basename "$app").zip"
  ditto -c -k --keepParent "$app" "$zip"
  local rc=0
  _notary_submit "$zip" || rc=1
  rm -rf "$tmp"
  [[ $rc -eq 0 ]] || return 1
  echo "[notarize] stapling $(basename "$app") ..." >&2
  xcrun stapler staple "$app"
  xcrun stapler validate "$app"
}
