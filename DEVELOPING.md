# Developing Errorta

## Prerequisites

| Tool | Version | Install |
|---|---|---|
| Node.js | ≥ 20 | https://nodejs.org/ |
| Rust | stable (≥ 1.77) | https://rustup.rs/ |
| Python | ≥ 3.10 | https://www.python.org/ |

On macOS you also need the Xcode Command Line Tools (`xcode-select --install`).

## Repository layout

```
Errorta/
  src/             — React + Vite + TypeScript frontend
  src-tauri/       — Tauri 2 (Rust) desktop shell
  python/          — Python sidecar (FastAPI) over AIAR
  ios/             — SwiftUI iPhone companion package
  docs/            — North Star, roadmap, feature specs
```

AIAR is consumed as an editable local dependency during development. The
expectation is that you have both repos checked out side-by-side:

```
~/GitHub/
  aiar/            — github.com/wiggins-j/aiar
  Errorta/         — this repo
```

## First-time setup

```bash
# Frontend deps
npm install

# Python sidecar deps (incl. AIAR editable install)
cd python
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pip install -e ../../aiar
cd ..
```

## Running in dev

You'll typically want two terminals open:

**Terminal 1 — Python sidecar:**

```bash
cd python
source .venv/bin/activate
python -m errorta_app.server
```

**Terminal 2 — Tauri + Vite (frontend):**

```bash
npm run tauri:dev
```

The Tauri window will open. The React frontend pings the sidecar every 5
seconds and surfaces its status in the UI.

## Demo boot

For the F031 Council click-through demo, use the one-liner:

```bash
./scripts/demo-up.sh             # Vite frontend (default — faster boot)
./scripts/demo-up.sh --tauri     # Tauri shell — same effect as DEMO_TAURI=1
```

The script:

1. Pre-flights `python3` / `node` / `npm` / `curl` (exits 3 if any is
   missing).
2. Boots `python -m errorta_app.server` in the background, capturing
   stdout/stderr to `.errorta-demo-logs/sidecar.log`.
3. Polls `/healthz` on a 30s budget, 250ms interval. On timeout or
   premature exit, dumps the last 40 lines of `sidecar.log` and exits
   4. If uvicorn is missing, the hint is printed.
4. Optionally probes the F-INFRA-11 welcome-corpus release URL
   (`--check-corpus`; respects `DEMO_OFFLINE=1`). Exits 5 on draft /
   non-200.
5. Boots the frontend dev server. If it exits within 1s of spawn,
   dumps the last 40 lines of `frontend.log` and exits 6 (most often:
   another Vite instance already holds `http://127.0.0.1:1420`).
6. Prints a single-line ready banner:

   ```
   READY  sidecar :8770  |  frontend http://127.0.0.1:1420
   ```

Ctrl-C (or any SIGINT / SIGTERM) traps cleanly: both child processes
receive SIGTERM and the script waits for them to exit. No orphans.

The script requires `uvicorn`. Install it with `pip install uvicorn`
inside the Python sidecar venv if you see exit 4 with an "uvicorn
missing" hint. The pytest smoke test
(`python/tests/test_sidecar_boot_smoke.py`) skips cleanly when uvicorn
is not available rather than failing.

Per the F031 demo plan, there is intentionally **no** `--seed` flag —
the operator clicking "Seed demo room" in the empty-state of the UI IS
the demo moment.

## Welcome corpus draft fallback

The F031 Council demo seeds the F007 welcome corpus on first use. The
tarball is pinned in `python/errorta_welcome/pinned_hash.json` and
`POST /welcome/install` (the empty-state one-shot) downloads it from
the `errorta-downloads` GitHub release feed.

Until F-INFRA-11 slice (e) un-drafts the `welcome-corpus-v0.1.0`
release on `wiggins-j/errorta-downloads`, the pinned
`releases/latest/download/welcome-corpus.tar.gz` URL may return 404 on
a clean machine. To unblock a demo before that ships:

1. **Build the tarball locally** via `bash scripts/build-welcome-corpus.sh`
   from the repo root. Produces a byte-stable `dist/welcome-corpus.tar.gz`.
   Then **POST it directly** to the F007 ingest endpoint, which bypasses
   `/welcome/install`'s network fetch entirely:

   ```
   curl -X POST http://127.0.0.1:8770/welcome/ingest \
     -H 'Content-Type: application/json' \
     -d '{"tarball_path": "'"$(pwd)"'/dist/welcome-corpus.tar.gz"}'
   ```

   See the pre-demo ingest step for
   the full operator flow. QA P2 #8 fix: the earlier claim that
   pre-staging at `python/errorta_welcome/welcome-corpus.tar.gz`
   short-circuits the downloader was wrong — `/welcome/install` always
   downloads to a fresh temp path; the `/welcome/ingest` route is what
   accepts a pre-staged tarball.

2. **Or set `DEMO_OFFLINE=1`** before running `scripts/demo-up.sh
   --check-corpus` to skip the reachability probe entirely. The
   pytest equivalent
   (`python/tests/test_welcome_corpus_release_reachable.py`) honors
   the same env var.

The demo-up script's `--check-corpus` flag (see "Demo boot" below)
prints the local fallback path on any non-200 HEAD.

## iOS companion

The iPhone companion starts as a native SwiftUI Swift package under
`ios/ErrortaCompanion`. It is intentionally a connector to a running desktop,
not a local AI runtime.

```bash
cd ios/ErrortaCompanion
swift test
open Package.swift
```

Open the package in Xcode for simulator work. The package currently includes
the core pairing payload parser, Keychain credential store, connection/error
models, and a minimal SwiftUI shell. A full signed iOS app target can be added
once TestFlight packaging begins.

## Building for release

The full multi-platform release is handled by CI (will be wired up in v0.5).
For local single-platform builds:

```bash
# 1. Build the Python sidecar binary
cd python
source .venv/bin/activate
pyinstaller sidecar.spec
cp dist/errorta-sidecar ../src-tauri/binaries/errorta-sidecar
cd ..

# 2. Build the Tauri bundle (signs on macOS if your Apple Developer
#    certificate is in the keychain)
npm run tauri:build
```

Outputs land in `src-tauri/target/release/bundle/`.

## Common tasks

| What | How |
|---|---|
| Type-check the frontend | `npm run lint` |
| Format Python | `ruff format python/` |
| Lint Python | `ruff check python/` |
| Run sidecar tests | `cd python && pytest` (once tests exist) |

## When something breaks

- **`npm run tauri:dev` fails with "no Rust toolchain"** → install rustup (`curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`) and `source $HOME/.cargo/env`.
- **Sidecar reports `aiar_available: false`** → you skipped the `pip install -e ../../aiar` step.
- **WebView2 not found on Windows** → install Microsoft Edge or grab the standalone WebView2 runtime.
