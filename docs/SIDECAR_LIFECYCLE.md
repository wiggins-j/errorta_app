# Sidecar lifecycle

How the Tauri shell spawns, monitors, and tears down the Python sidecar.

## Components

- **`src-tauri/src/sidecar.rs`** — `SidecarHandle` (managed state) + spawn/
  terminate/restart helpers + the `sidecar_port`, `restart_sidecar`, and
  `processes` Tauri commands.
- **`src-tauri/src/lib.rs`** — registers the handle and the commands, kicks
  off `spawn_sidecar` on a background thread in `setup()`, and terminates
  the child on window-close / app-exit.
- **`scripts/build-sidecar.sh`** — runs PyInstaller and stages the binary
  under `src-tauri/binaries/errorta-sidecar-<target-triple>` so Tauri's
  `externalBin` machinery can find it.
- **`python/sidecar_main.py`** — PyInstaller entry shim; delegates to
  `errorta_app.server.main()` which honors `ERRORTA_SIDECAR_PORT`.
- **`src/lib/sidecarPort.ts`** — frontend resolver; calls the
  `sidecar_port` command and caches the result.
- **`src/lib/api.ts`** — every request goes through `sidecarFetch`, which
  awaits `getSidecarBase()` before issuing fetch.

## Startup sequence

1. Rust shell boots, registers `SidecarHandle::new()` as managed state.
2. `setup()` spawns a thread that calls `spawn_sidecar(app_handle)`.
3. `spawn_sidecar` allocates a free port via `TcpListener::bind("127.0.0.1:0")`,
   then runs the `errorta-sidecar` external binary with
   `ERRORTA_SIDECAR_PORT=<port>` in its env.
4. The child's stdout/stderr are drained on a Tokio task into the Rust
   shell's stderr (prefixed `[sidecar/out]`, `[sidecar/err]`).
5. `wait_for_healthz` polls `/healthz` for up to 10 seconds. On success the
   port is stored in the `SidecarHandle`; on failure the child is killed and
   the handle stays at port 0.

## Frontend access

```ts
import { sidecarHealth } from "@/lib/api";
const health = await sidecarHealth(); // awaits getSidecarBase() under the hood
```

`getSidecarBase()` caches the resolved base in a module-level promise.
Call `resetSidecarBaseCache()` after `restart_sidecar` so the next request
picks up the new port.

## Restart

```ts
import { invoke } from "@tauri-apps/api/core";
import { resetSidecarBaseCache } from "@/lib/sidecarPort";

const newPort = await invoke<number>("restart_sidecar");
resetSidecarBaseCache();
```

`restart_sidecar` terminates the existing child, allocates a fresh port,
and re-runs the spawn+healthz sequence.

## Teardown

Two paths converge on `SidecarHandle::terminate()`:

- `WindowEvent::CloseRequested` on the main window.
- `RunEvent::ExitRequested` / `RunEvent::Exit` (the app process is going
  away).

`terminate()` takes the `Option<CommandChild>` out of the `Mutex`, calls
`kill()` on it, and zeroes the stored port. It is safe to call when no
child is running.

## Dev-mode fallback

When running `vite` directly (no Tauri shell) or when the PyInstaller
binary hasn't been built yet, `spawn_sidecar` returns an error and the
frontend's `getSidecarBase()` falls back to `http://127.0.0.1:8770`. Run
the sidecar manually:

```bash
cd python && source .venv/bin/activate && python -m errorta_app.server
```

## Notes

- The free-port allocation has a small race between drop and re-bind. We
  accept it; the same pattern is used by Vite, Next.js, etc.
- **Sidecar logging (Spec 22).** The two front-ends differ, deliberately.
  - **CLI-spawned** — `errorta_cli.sidecar._launch` opens
    `${ERRORTA_HOME}/logs/sidecar.log` and hands that fd to the child as both
    stdout *and* stderr (merged: a traceback split across two files is worse
    than either). The fd is inherited by the detached child and outlives the
    CLI process, so a sidecar that dies during startup leaves its traceback
    behind instead of only an exit code. Rotation happens **at spawn** — the
    child holds the fd for its whole life, so nothing can rotate underneath a
    running sidecar: past 8 MB the file is renamed to `sidecar.log.1`
    (displacing any previous `.1`) and a fresh one is opened. Exactly two
    generations, 16 MB total, forever. An unwritable `logs/` fails **open**:
    the CLI warns once on stderr and falls back to `DEVNULL`.
  - **App-spawned** — Tauri still drains the sidecar's pipes to the Rust
    shell's stderr (`src-tauri/src/sidecar.rs`). Item 1's fd redirect is
    CLI-only, so the two never double-write.
  - **Both** — `errorta_app.sidecar_log` installs a **redacting**, byte-capped
    `logging` handler on the root and `uvicorn.*` loggers writing to the same
    `logs/sidecar.log`. It runs the pipeline `/diagnostics/log-tail` already
    applies, so `sk-…` / `sk-ant-…`-shaped tokens never reach disk. This is the
    only half that covers an **adopted** sidecar, whose fds belong to whoever
    spawned it and cannot be re-pointed. In the CLI case the console handlers
    are detached while it is installed, so each structured line is written once
    — and redacted. On crossing its budget the handler logs one warning and
    stops writing (truncating in place is rejected: the CLI's fd points at the
    inode).
  - `ERRORTA_LOG_FILE` is unrelated and unchanged: opt-in, and **unredacted**.
  - An in-app log viewer is still a v0.5 problem; the file is the v0.1 answer.
- The `processes` command currently reports only the sidecar. Future work
  (Ollama supervisor) will extend it.
