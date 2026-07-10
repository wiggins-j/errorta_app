// Resolves the sidecar base URL.
//
// In a Tauri build the Rust shell allocates a free port at startup, spawns
// the PyInstaller `errorta-sidecar` binary on it, and exposes the port via
// the `sidecar_port` Tauri command. In browser-dev (`vite` only, no Tauri
// window) we fall back to the fixed `SIDECAR_BASE` so devs can run
// `python -m errorta_app.server` manually on 8770.
//
// The resolved base is cached as a module-level promise so concurrent
// callers share one round-trip to the Tauri backend.
import { SIDECAR_BASE } from "./api";

let cached: Promise<string> | null = null;

function isTauri(): boolean {
  // Tauri 2 injects `window.__TAURI_INTERNALS__`. The public `invoke`
  // re-export from `@tauri-apps/api/core` works in both Tauri and browser
  // contexts — in browser it throws synchronously, which we catch below.
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/** True when running inside the Tauri webview (vs. plain `vite` browser dev). */
export function isTauriRuntime(): boolean {
  return isTauri();
}

/**
 * Best-effort invoke of a Tauri command. Returns null in browser-dev or if the
 * command bridge is unavailable / the command rejects. Used by the F103 startup
 * gate's recovery actions, which must never throw into the splash.
 */
export async function tauriInvoke<T>(cmd: string): Promise<T | null> {
  if (!isTauri()) return null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    return await invoke<T>(cmd);
  } catch {
    return null;
  }
}

/** A Tauri `invoke` bound to `@tauri-apps/api/core`, accepting command args. */
export type TauriInvoke = <T>(
  cmd: string,
  args?: Record<string, unknown>,
) => Promise<T>;

/**
 * Resolve the real Tauri `invoke` function, or `null` when not running inside
 * the Tauri webview. Callers that need to distinguish "no Tauri shell" from "the
 * command threw" get exactly that: `null` means browser-dev; a returned function
 * propagates command errors to the caller's own try/catch.
 *
 * IMPORTANT: this uses a bundler-RESOLVED `await import("@tauri-apps/api/core")`.
 * The previous per-file `new Function("s","return import(s)")` shim dodged the
 * bundler, so `@tauri-apps/api/core` never shipped in the packaged app and every
 * invoke silently no-oped in production (the SSH-test "browser-dev" bug, plus
 * residency switching, CLI login, and the updater). Same failure + fix as
 * `FilePickerDialog.tsx`. Do not reintroduce the `new Function` form.
 */
export async function loadTauriInvoke(): Promise<TauriInvoke | null> {
  if (!isTauri()) return null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    return invoke as TauriInvoke;
  } catch {
    return null;
  }
}

async function resolve(): Promise<string> {
  if (!isTauri()) return SIDECAR_BASE;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const port = await invoke<number>("sidecar_port");
    if (typeof port === "number" && port > 0) {
      return `http://127.0.0.1:${port}`;
    }
  } catch {
    // Fall through — the command may not be registered yet, or the sidecar
    // hasn't finished booting. Caller will retry by resetting the cache.
  }
  return SIDECAR_BASE;
}

export function getSidecarBase(): Promise<string> {
  if (!cached) cached = resolve();
  return cached;
}

export function resetSidecarBaseCache(): void {
  cached = null;
}

// F103 — a startup-safe resolver that, unlike `getSidecarBase()`, does NOT
// collapse "Tauri sidecar still starting" into the dev fallback. The startup
// gate needs to tell these apart so it keeps waiting (and never caches
// `127.0.0.1:8770` for a packaged Tauri build whose ephemeral sidecar port
// hasn't been published yet).
export type SidecarBaseResolution =
  | { kind: "tauri"; base: string; port: number }
  | { kind: "tauri-starting" }
  | { kind: "browser-dev"; base: string };

export async function resolveSidecarBase(): Promise<SidecarBaseResolution> {
  if (!isTauri()) return { kind: "browser-dev", base: SIDECAR_BASE };
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const port = await invoke<number>("sidecar_port");
    if (typeof port === "number" && port > 0) {
      return { kind: "tauri", base: `http://127.0.0.1:${port}`, port };
    }
  } catch {
    // The command may not be registered yet during very early boot. In a Tauri
    // window that is transient — treat it as "still starting" so we keep
    // polling instead of falling back to the dev port.
  }
  return { kind: "tauri-starting" };
}

// F103 — coarse lifecycle snapshot from the Rust shell (`sidecar_startup_state`).
// Returns null in browser-dev or if the command is unavailable.
export interface StartupSnapshot {
  state: "not_started" | "starting" | "healthy" | "failed" | "terminated";
  port: number;
  elapsedMs: number;
  lastError: string | null;
}

export async function getStartupSnapshot(): Promise<StartupSnapshot | null> {
  if (!isTauri()) return null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const raw = await invoke<{
      state?: string;
      port?: number;
      elapsed_ms?: number;
      last_error?: string | null;
    }>("sidecar_startup_state");
    const state = raw?.state;
    if (
      state !== "not_started" &&
      state !== "starting" &&
      state !== "healthy" &&
      state !== "failed" &&
      state !== "terminated"
    ) {
      return null;
    }
    return {
      state,
      port: typeof raw.port === "number" ? raw.port : 0,
      elapsedMs: typeof raw.elapsed_ms === "number" ? raw.elapsed_ms : 0,
      lastError: typeof raw.last_error === "string" ? raw.last_error : null,
    };
  } catch {
    return null;
  }
}

// F063 A2: ask the Rust shell to ENSURE a live sidecar (respawn if its child
// died) and return that port. Used by the frontend self-heal after a transport
// failure — unlike `getSidecarBase`, this can recover a dead sidecar rather
// than just re-reading a possibly-stale port. Browser-dev (no Tauri) falls
// back to the fixed dev base.
export async function ensureSidecarBase(): Promise<string> {
  if (!isTauri()) {
    cached = Promise.resolve(SIDECAR_BASE);
    return SIDECAR_BASE;
  }
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const port = await invoke<number>("ensure_sidecar");
    if (typeof port === "number" && port > 0) {
      const base = `http://127.0.0.1:${port}`;
      cached = Promise.resolve(base);
      return base;
    }
  } catch {
    // Respawn failed — fall through to the dev fallback; the caller's retry
    // will then surface a clear "backend not responding" error.
  }
  cached = Promise.resolve(SIDECAR_BASE);
  return SIDECAR_BASE;
}
