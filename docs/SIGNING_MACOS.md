# macOS signing & notarization

Credential-setup reference for the local release pipelines
([`scripts/release-macos.sh`](../scripts/release-macos.sh) for the `.dmg` app and
[`scripts/release-cli.sh`](../scripts/release-cli.sh) for the Homebrew CLI). All
signing happens **locally on the maintainer's Mac** — there is no CI (locked
decision). The runbook that drives these scripts is
[`docs/BUILD_AND_RELEASE.md`](BUILD_AND_RELEASE.md); this file is the one-time
credential setup they both assume.

> **Do I even need this?** For the **Homebrew CLI**, no — `brew install
> errorta/tap/errorta` works with an unsigned (ad-hoc) binary, because Homebrew
> fetches the tarball with its own `curl`, which does **not** quarantine the
> download, so Gatekeeper never runs its online notarization check. See
> [When you can skip signing](#when-you-can-skip-signing). You need everything
> below for the **`.dmg` app** and for the (optional) hardened CLI build that
> also survives a **direct browser download** off the Releases page.

---

## Prerequisites

1. **Apple Developer Program membership** (paid). Developer ID certificates and
   notarization are not available on a free account.
2. **Xcode Command Line Tools** — provides `codesign`, `security`, and
   `xcrun notarytool` / `xcrun stapler`:
   ```sh
   xcode-select --install
   ```
3. A **Developer ID Application** certificate (this is the certificate type for
   distributing signed software *outside* the Mac App Store — not "Apple
   Development" or "Mac App Distribution").

---

## 1. The signing identity

### Create / install the certificate

Create a **Developer ID Application** certificate at
<https://developer.apple.com/account/resources/certificates> (or via Xcode →
Settings → Accounts → Manage Certificates → **+** → Developer ID Application),
then download and double-click the `.cer` to import it into your **login**
keychain. The matching private key must live in the same keychain (it does
automatically if you created the CSR on this Mac).

### Find the identity string

```sh
security find-identity -v -p codesigning
```

Look for the line reading `Developer ID Application: <Your Name> (<TEAMID>)`.
The quoted portion — e.g. `Developer ID Application: EXAMPLE (TEAMID)` — is your
**signing identity string**.

### Store it for the release scripts

Both release scripts read `APPLE_SIGNING_IDENTITY` from the environment or from
`~/.config/errorta-release.env` (which they `source`). Create that file:

```sh
mkdir -p ~/.config
cat > ~/.config/errorta-release.env <<'EOF'
export APPLE_SIGNING_IDENTITY="Developer ID Application: EXAMPLE (TEAMID)"
EOF
chmod 600 ~/.config/errorta-release.env
```

Never hard-code the identity in the repo — it is always read from the
environment or this untracked file.

**Two derived variables** you normally don't set (defaults are correct), but may
see referenced in the scripts and `python/cli.spec`:

| Variable | Default | Purpose |
|---|---|---|
| `ERRORTA_CODESIGN_IDENTITY` | falls back to `APPLE_SIGNING_IDENTITY` | identity PyInstaller uses to sign the binary during assembly |
| `ERRORTA_ENTITLEMENTS_PLIST` | the committed `src-tauri/macos/entitlements.plist` | hardened-runtime entitlements applied at codesign |

---

## 2. Notarization credentials

Notarization submits the signed artifact to Apple's notary service so Gatekeeper
trusts it. [`scripts/lib/notarize.sh`](../scripts/lib/notarize.sh) detects
credentials in this order:

### Preferred: a keychain profile (`errorta-notary`)

Store credentials once; they live in the keychain and no secret is ever passed
on a command line again:

```sh
xcrun notarytool store-credentials errorta-notary \
  --apple-id "you@example.com" \
  --team-id "TEAMID" \
  --password "<app-specific-password>"
```

The scripts detect this profile with a **liveness probe**
(`xcrun notarytool history --keychain-profile errorta-notary`), not a keychain
service-name lookup — so it is reported present only when it actually works.

### Fallback: environment variables

If the profile is absent, the scripts use:

```sh
export APPLE_ID="you@example.com"
export APPLE_TEAM_ID="TEAMID"
export APPLE_APP_SPECIFIC_PASSWORD="<app-specific-password>"
```

(These can also live in `~/.config/errorta-release.env`.)

### App-specific password

The `--password` above is **not** your Apple ID password. Mint an
app-specific password at <https://appleid.apple.com> → Sign-In and Security →
App-Specific Passwords. It looks like `abcd-efgh-ijkl-mnop`.

---

## 3. CLI vs. `.dmg`: notarize, and when to staple

| Artifact | Signed | Notarized | Stapled |
|---|---|---|---|
| `.dmg` app (`release-macos.sh`) | Developer ID | yes | **yes** — a disk image can hold the ticket |
| CLI binary (`release-cli.sh`) | Developer ID (or ad-hoc) | optional | **no** — a bare Mach-O cannot be stapled |

A bare Mach-O executable cannot be stapled (`xcrun stapler` only handles
`.app` / `.dmg` / `.pkg` bundles). The CLI pipeline therefore codesigns with a
hardened runtime, zips the binary, submits the **zip** to `notarytool`, and does
**not** staple. Gatekeeper verifies notarization **online** the first time a
*quarantined* copy runs.

### When you can skip signing

For the **Homebrew** install path, signing/notarization is not required:

- Homebrew downloads the tarball with `curl`, which does not set the
  `com.apple.quarantine` xattr, so Gatekeeper's online check never fires.
- PyInstaller **ad-hoc-signs** the arm64 binary by default (when
  `ERRORTA_CODESIGN_IDENTITY` is unset), and an ad-hoc signature is all Apple
  Silicon needs to *execute* a binary.

So `release-cli.sh --skip-notarize` produces a binary that installs and runs via
`brew` with **no Apple Developer credentials at all**. Do the full Developer ID +
notarization pass when you also want the tarball to survive a **direct browser
download** from the GitHub Releases page (a browser *does* quarantine it), or as
defense-in-depth. The `.dmg` app always needs the full pass.

---

## 4. Verify & troubleshoot

```sh
# Is the signing identity present and valid for codesigning?
security find-identity -v -p codesigning | grep "Developer ID Application"

# Does the notary keychain profile work? (non-zero exit => not set up)
xcrun notarytool history --keychain-profile errorta-notary

# Inspect a signed binary.
codesign --verify --strict --verbose=2 dist/errorta
codesign --display --entitlements - dist/errorta
```

Common failures:

- **`signing identity not in codesigning keychain`** — the string in
  `APPLE_SIGNING_IDENTITY` doesn't match any line from `security find-identity`.
  Copy the exact quoted string, including `(TEAMID)`.
- **`no notarization credentials`** — neither the `errorta-notary` profile nor
  all three `APPLE_ID` / `APPLE_TEAM_ID` / `APPLE_APP_SPECIFIC_PASSWORD` are
  available. Set up one of them (above).
- **notarization status not `Accepted`** — `notarize.sh` prints the notary log;
  the usual cause is a hardened-runtime/entitlements mismatch or an unsigned
  nested binary.
- **works locally, but a downloaded copy is blocked** — that copy is
  quarantined; it needs a notarized (Developer ID) build, not just ad-hoc. This
  does not affect `brew install` (see [above](#when-you-can-skip-signing)).

---

*All identifiers here are placeholders (`EXAMPLE`, `TEAMID`, `you@example.com`).
Substitute your own; keep real values only in the untracked
`~/.config/errorta-release.env` and the keychain.*
