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
- v0.1 logs sidecar stdout/stderr straight to the Rust shell's stderr.
  Structured logging + an in-app log viewer is a v0.5 problem.
- The `processes` command currently reports only the sidecar. Future work
  (Ollama supervisor) will extend it.
