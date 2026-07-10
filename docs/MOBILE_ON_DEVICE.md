# Testing the iPhone companion on a real device (F065/F066)

The companion talks to your Mac over the **LAN, over TLS**, after a
**desktop-approved** pairing. The connector is **off by default**.

## 0. Prerequisites
- iPhone and Mac on the **same Wi-Fi**.
- A paid Apple Developer account (for year-long device provisioning).
- Xcode + (for regenerating the project) `xcodegen`.

## 1. Desktop: enable the LAN connector
Find your Mac's LAN IP (System Settings → Wi-Fi → Details → IP address, e.g.
`192.0.2.42`). Enable the connector (Settings → Mobile connector, or via the
sidecar API on loopback):

```
PUT /settings/mobile-connector
{ "enabled": true, "bind_mode": "lan", "lan_bind_address": "192.0.2.42",
  "port": 8788, "require_tls": true, "pairing_enabled": true }
```

The response includes `lan_listener.cert_sha256` — **note this fingerprint**.
A self-signed TLS cert is generated under `~/.errorta/mobile/tls/` and the
listener starts on `https://192.0.2.42:8788` serving **only** `/mobile/v1/*`.

## 2. Desktop: start a pairing
`POST /settings/mobile-connector/pairing/start` → returns a `pairing_payload`
with the `pairing_token`, `hosts`, `port`, and `tls_cert_sha256`. Keep these
for the phone (a QR encoder is a later slice; for now enter them manually).

## 3. Build + install to your iPhone

For the fast CLI redeploy loop (build + sign + install in one shot, no Xcode
window) and the full troubleshooting table, see
[`ios/ErrortaCompanion/README.md`](../ios/ErrortaCompanion/README.md). The
short version with Xcode:

```
cd ios/ErrortaCompanion
xcodegen generate          # if the .xcodeproj isn't already present
open ErrortaCompanion.xcodeproj
```
- Select the **ErrortaCompanion** target → Signing & Capabilities → pick your
  **Team** (automatic signing). Bundle id `app.errorta.companion`.
- Select your iPhone as the run destination → **Run**.
- First launch on the device: trust the developer profile at
  Settings → General → VPN & Device Management.

## 4. iPhone: pair
- Enter the server URL `https://<mac-ip>:<port>`, the `pairing_token`, and the
  `tls_cert_sha256` fingerprint.
- The app posts `complete_pairing`; the desktop shows a pending device —
  **approve it** (Settings → Mobile connector, or
  `POST /settings/mobile-connector/pairing/{session_id}/approve`).
- The app polls `pairing/status`, receives its session token once, and stores
  it in the iOS Keychain. A freshly-paired device is **read-only**
  (`read_runs`) — grant `start_runs` / `send_messages` / approvals from the
  desktop device list when you want them.

## Notes
- **Local Network permission** prompts on first connection — allow it. If
  dismissed: Settings → Privacy & Security → Local Network → ErrortaCompanion.
- Must be a **physical device** (the iOS Simulator routes the network
  differently for LAN/pinned-TLS).
- The TLS cert is pinned by its **DER SHA-256** — the value the desktop showed
  in step 1. A mismatch is rejected (no ATS exception, no plaintext).
- Disable any time: `PUT /settings/mobile-connector {"enabled": false}` stops
  the listener.
