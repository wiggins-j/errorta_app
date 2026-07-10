# Errorta (iOS)

The iPhone app for Errorta. Talks to the desktop over the **LAN, over TLS**,
after a **desktop-approved** pairing (see
[`docs/MOBILE_ON_DEVICE.md`](../../docs/MOBILE_ON_DEVICE.md) for the full
enable → pair → approve flow).

The Xcode project, scheme, Swift package, and bundle identifier still use the
`ErrortaCompanion` internal name for continuity. The installed app displays as
**Errorta**.

This README covers **building + deploying the app to a physical iPhone** — the
exact CLI path, so you can redeploy in one shot without fighting the Xcode UI.

## One-time setup

- **Paid Apple Developer account**, with your team id set in
  [`project.yml`](project.yml) (`DEVELOPMENT_TEAM`). It's already set to the
  maintainer's team; change it if you fork.
- **Xcode** + `xcodegen` (`brew install xcodegen`).
- **Developer Mode ON on the iPhone** — this is the step that bites you first.
  On iOS 16+ Xcode cannot install to the device until you enable it:
  Settings → Privacy & Security → **Developer Mode** → toggle on → the phone
  restarts → after reboot confirm **Turn On**. (The row only appears after the
  phone has connected to Xcode at least once.)
- Plug the phone in, **unlock it**, and tap **Trust** on "Trust This Computer?".

## Fast redeploy (CLI — what we actually use)

Run from this directory (`ios/ErrortaCompanion/`). It regenerates the project
(picks up any `project.yml` / asset changes), builds + signs for the connected
device, and installs it — no Xcode window needed.

```bash
cd ios/ErrortaCompanion

# 1. Regenerate the .xcodeproj (only needed after project.yml / Assets changes).
xcodegen generate

# 2. Find the connected iPhone's hardware UDID (xcodebuild's -destination id).
UDID=$(xcrun xctrace list devices 2>&1 \
  | sed -n '/== Devices ==/,/== Simulators ==/p' \
  | grep -iE "iphone" | grep -v "Offline" \
  | sed -E 's/.*\(([0-9A-Fa-f-]{25,})\).*/\1/' | head -1)
echo "device UDID: $UDID"

# 3. Build + sign for the device (-allowProvisioningUpdates lets Xcode mint the
#    provisioning profile automatically the first time).
xcodebuild build \
  -project ErrortaCompanion.xcodeproj \
  -scheme ErrortaCompanion \
  -destination "id=$UDID" \
  -allowProvisioningUpdates

# 4. Install the freshly-built .app onto the phone via devicectl.
#    (devicectl uses its OWN identifier, not the hardware UDID — discover it.)
DEVID=$(xcrun devicectl list devices 2>/dev/null \
  | grep -i "iphone" | grep -i "connected" \
  | grep -oE '[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}' | head -1)
APP=$(find ~/Library/Developer/Xcode/DerivedData/ErrortaCompanion-*/Build/Products/Debug-iphoneos \
  -maxdepth 1 -name "Errorta.app" 2>/dev/null | head -1)
xcrun devicectl device install app --device "$DEVID" "$APP"
```

That's the whole loop. After the first install, if the app shows **"Untrusted
Developer"**, trust the profile once: Settings → General →
**VPN & Device Management** → your developer profile → **Trust**.

> **Two different device ids, on purpose.** `xcodebuild`'s `-destination id=`
> wants the **hardware UDID** (e.g. `00008110-…`); `devicectl … --device` wants
> the **devicectl identifier** (a different UUID). The snippet discovers each
> with the right tool, so don't hardcode either.

### Or just use Xcode

Open `ErrortaCompanion.xcodeproj`, pick your iPhone as the run destination, and
hit **Run** (▶). Signing is automatic (team is pinned in `project.yml`).

## Common failures

| Symptom | Fix |
|---|---|
| `…is not available because the Developer Disk Image is not mounted` | **Developer Mode is off** (or phone locked). Enable it (see setup) and unlock. |
| `Timed out waiting for all destinations…` | Phone locked / not trusted / mid-provisioning. Unlock, re-run; the first device build is slow. |
| Signing errors after `xcodegen generate` | `DEVELOPMENT_TEAM` in `project.yml` must be set — `xcodegen` regenerates from it. |
| Home-screen icon didn't update | iOS caches the old icon. Delete the app on the phone and reinstall. |

## Layout

- `Sources/ErrortaCompanionApp/` — the SwiftUI iOS app target (entry, `RootView`,
  `Assets.xcassets` → `AppIcon` + `BrandLogo`, the pixel-art "E" cloud shared
  with the desktop app).
- `Sources/ErrortaCompanionCore/` — SwiftPM library: pairing payloads, TLS
  cert pinning (`PinnedSession.swift`), Keychain store, mobile API client,
  event/approval/inbox projections. Unit-tested under `Tests/`.
- `project.yml` — XcodeGen spec. Edit this, then `xcodegen generate`; never
  hand-edit the `.xcodeproj`.
