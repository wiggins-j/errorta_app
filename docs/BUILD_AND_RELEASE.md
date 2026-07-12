# Build & release runbook

Canonical maintainer runbook for producing Errorta release artifacts. All builds
run **locally on the maintainer's hardware** — GitHub Actions is off (locked
decision).

- macOS app (`.dmg`): `scripts/release-macos.sh <tag> [--alpha]`.
- CLI binary (Homebrew): `scripts/release-cli.sh` — documented below.

Signing / notarization credential setup for both: `docs/SIGNING_MACOS.md`.

---

## Releasing the `errorta` CLI (Homebrew)

The `errorta` CLI ships as a **single self-contained binary** built by
`pyinstaller python/cli.spec` (it embeds the sidecar + the AIAR engine, so a user
needs no Python and no other deps). Distribution is a Homebrew tap:

```sh
brew install errorta/tap/errorta
```

`scripts/release-cli.sh` is the per-platform pipeline: **build → (macOS) sign +
notarize → tarball + sha256 → upload to the `errorta_app` GitHub Release → update
the tap formula.** The version is read from `python/pyproject.toml` (the single
source of truth) and drives the git tag (`cli-vX.Y.Z`), the tarball name, and the
formula `version`.

### Why it's per-platform

PyInstaller only builds for the **host OS/arch**, so this script releases **one
platform per run**. Run it once on each target and the tap formula accumulates
all of them (each run preserves the other platforms' already-published `sha256`
values — see "How other-arch values are preserved" below).

Build matrix:

| Platform | Where you run it | Asset produced |
|---|---|---|
| macOS arm64 | Apple-silicon Mac | `errorta-X.Y.Z-darwin-arm64.tar.gz` |
| macOS x86_64 | Intel Mac, or arm64 Mac under Rosetta / with `--target-arch universal2` in the spec | `errorta-X.Y.Z-darwin-x86_64.tar.gz` |
| Linux x86_64 | Linux box or container | `errorta-X.Y.Z-linux-x86_64.tar.gz` |

(A single **universal2** macOS binary is an option — simpler formula, larger
file. If you go that route, build it as the `darwin-arm64` asset or add a
universal slot; the formula template has separate arm/intel slots today.)

Windows is out of scope for Homebrew (brew is macOS/Linux only); a Windows build
+ `scoop`/`winget` is a separate later track.

### Prerequisites

- **PyInstaller** in `python/.venv` (`pip install -e python[dev]`) or on `PATH`.
- **`gh` CLI**, authenticated (`gh auth login`) — for the release upload.
- **macOS signing — optional for `brew`, required for direct downloads.**
  `brew install` fetches the tarball with `curl`, which does **not** quarantine
  it, so an ad-hoc-signed binary (PyInstaller's default) installs and runs fine —
  no Apple credentials needed (`--skip-notarize`). Do the full **Developer ID +
  notarization** pass only when you also want the tarball to survive a **browser
  download** off the Releases page (a browser *does* quarantine): a Developer ID
  Application identity in `APPLE_SIGNING_IDENTITY` (or
  `~/.config/errorta-release.env`) plus notarization credentials (the
  `errorta-notary` keychain profile, or `APPLE_ID` / `APPLE_TEAM_ID` /
  `APPLE_APP_SPECIFIC_PASSWORD`). Full setup: `docs/SIGNING_MACOS.md`.
- A **local clone of the tap** (`errorta/homebrew-tap`) if you want the script to
  update the formula.
- The release repo (`wiggins-j/errorta_app`) Releases must be **public** (brew
  downloads them anonymously), and you need push access to both it and the tap.

Validate all of the above without building via `bash scripts/release-cli.sh
--check [--tap-dir …] [--skip-notarize]` (add `--online` to also probe the
notary credentials — a network round-trip).

### One-time: clone the tap

```sh
git clone https://github.com/errorta/homebrew-tap.git ~/GitHub/homebrew-tap
```

### Invocations

Preview first — `--dry-run` prints every step and the rendered formula without
building, uploading, or pushing:

```sh
bash scripts/release-cli.sh --dry-run --tap-dir ~/GitHub/homebrew-tap
```

Then, on **each platform**, in order (any order works; each run only fills its
own arch and preserves the others):

```sh
# macOS arm64 — build, sign+notarize, upload, update+push the formula
bash scripts/release-cli.sh --tap-dir ~/GitHub/homebrew-tap --push-tap

# macOS x86_64 (Intel Mac, or Rosetta) — same
bash scripts/release-cli.sh --tap-dir ~/GitHub/homebrew-tap --push-tap

# Linux x86_64 (no signing; notarization is auto-skipped)
bash scripts/release-cli.sh --tap-dir ~/GitHub/homebrew-tap --push-tap
```

If you'd rather review the formula before it goes live, drop `--push-tap`: the
script writes `Formula/errorta.rb` but leaves the commit/push to you.

### Flags

| Flag | Effect |
|---|---|
| `--version X.Y.Z` | Override the version (default: `python/pyproject.toml`). |
| `--tap-dir PATH` | Local clone of `errorta/homebrew-tap`; render the formula into it. Omit to skip formula work entirely. |
| `--push-tap` | After rendering, `git add`/`commit`/`push` the tap. Requires `--tap-dir`. |
| `--skip-notarize` | Skip macOS codesign + notarization (produces an **ad-hoc-signed** binary — installs+runs via brew, but a browser download is Gatekeeper-blocked). Auto-set on Linux. |
| `--check` | Validate prerequisites and exit **without building**. Add `--online` to also probe notary credentials. |
| `--dry-run` | Print the plan (and the pruned formula); build/upload/push nothing. |

### How other-arch values are preserved

Every platform's `url` is deterministic (derived from the version + tag), so the
script always writes all three URLs. Only the `sha256` needs the actual build.
On a per-platform run the script looks up the **other** platforms' `sha256`
values in the current formula by matching the **version-stamped asset name**
(`errorta-<VERSION>-<os>-<arch>.tar.gz`) and carries them forward. Because the
lookup keys on the *new* version's asset name, an old-version formula won't match
— so bumping the version correctly resets the other arches to `@@…_SHA@@`
placeholders until each one's own run lands (no stale shas leak into a new
release). A formula still carrying a placeholder for an arch means that arch
hasn't been built for the current version yet.

### macOS notarization: notarize, don't staple (bare binary)

The CLI is a bare Mach-O executable, and **a bare Mach-O cannot be stapled** —
`xcrun stapler` only works on `.app` / `.dmg` / `.pkg` bundles. So the script:

1. `codesign`s the binary with the Developer ID identity + **hardened runtime**
   (`--options runtime`, using `src-tauri/macos/entitlements.plist`);
2. zips the signed binary and submits the **zip** to `notarytool` (reusing
   `scripts/lib/notarize.sh`'s credential detection);
3. **does not staple** — there's no ticket to attach to a standalone binary.

Gatekeeper verifies notarization **online** the first time the extracted binary
runs, which is sufficient for a downloaded CLI. (This differs from the `.dmg`
flow in `release-macos.sh`, which *does* staple because a disk image can hold a
ticket.)

### Binary size

The tarball is large — **~100–200 MB** — because the binary bundles AIAR,
chromadb, sentence-transformers, and uvicorn. Homebrew handles it fine; the
release notes call it out so users aren't surprised.

### After the release

- Verify: `brew install errorta/tap/errorta && errorta --help`.
- The PyPI path (`pip install errorta-cli`) is a **separate, later** track gated
  on AIAR being published to PyPI — see the F148 spec §5.
