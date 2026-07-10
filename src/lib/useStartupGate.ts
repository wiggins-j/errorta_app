// F103 — root startup gate. Owns the cold-launch lifecycle state machine that
// keeps the full-window splash up until the local sidecar has returned
// `/healthz` once (or the user opts into limited mode after a failure).
//
// Why this lives outside the Shell feature: it gates the ENTIRE app, so it must
// run before the sidebar/feature panes mount. It deliberately uses the
// startup-safe `resolveSidecarBase()` (never caches the dev fallback while a
// Tauri sidecar is still selecting its ephemeral port) and reads the Rust
// lifecycle snapshot (`sidecar_startup_state`) for honest failure copy instead
// of inferring failure from `port == 0` + failed HTTP.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  getStartupSnapshot,
  resetSidecarBaseCache,
  resolveSidecarBase,
  tauriInvoke,
} from "./sidecarPort";

export type StartupPhase =
  | "opening_shell"
  | "waiting_for_port"
  | "waiting_for_healthz"
  | "ready"
  | "failed"
  | "limited";

export type StartupMode = "loading" | "ready" | "failed" | "limited";

export interface StartupState {
  phase: StartupPhase;
  elapsedMs: number;
  sidecarPort: number | null;
  developerMode: boolean;
  lastError: string | null;
}

export interface StartupActions {
  /** Reset the base cache, ask Tauri to ensure a fresh sidecar, restart polling. */
  retry: () => void;
  /** Open the local logs folder (never uploads). */
  openLogs: () => Promise<void>;
  /** Enter the shell with backend marked not-ready (recovery path). */
  openLimited: () => void;
  /** Fully quit the app (tears down the sidecar via RunEvent::Exit). */
  quit: () => Promise<void>;
}

export interface StartupGate {
  mode: StartupMode;
  state: StartupState;
  actions: StartupActions;
}

// Frontend failure budget. A small margin over the Rust `HEALTHZ_TIMEOUT`
// (120s) so a slow-but-healthy boot still wins; explicit `failed`/`terminated`
// from the lifecycle snapshot fails faster than this.
const FAILURE_BUDGET_MS = 135_000;
// Steady poll cadence once the window is up.
const POLL_INTERVAL_MS = 1_000;
// Per-attempt `/healthz` timeout so a hung socket can't stall the loop.
const HEALTH_TIMEOUT_MS = 4_000;

async function healthOk(base: string): Promise<boolean> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), HEALTH_TIMEOUT_MS);
  try {
    const r = await fetch(`${base}/healthz`, {
      method: "GET",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    return r.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

export function useStartupGate(): StartupGate {
  const [mode, setMode] = useState<StartupMode>("loading");
  const [phase, setPhase] = useState<StartupPhase>("opening_shell");
  const [elapsedMs, setElapsedMs] = useState(0);
  const [sidecarPort, setSidecarPort] = useState<number | null>(null);
  const [developerMode, setDeveloperMode] = useState(false);
  const [lastError, setLastError] = useState<string | null>(null);
  // Bumped by retry to restart the polling effect.
  const [runId, setRunId] = useState(0);

  // Tracks the live run so async continuations from a stale run are dropped.
  const runRef = useRef(0);

  useEffect(() => {
    runRef.current = runId;
    const myRun = runId;
    const startedAt = Date.now();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let elapsedTimer: ReturnType<typeof setInterval> | null = null;
    const alive = () => runRef.current === myRun;

    elapsedTimer = setInterval(() => {
      if (alive()) setElapsedMs(Date.now() - startedAt);
    }, POLL_INTERVAL_MS);

    const stopTimers = () => {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
      if (elapsedTimer !== null) {
        clearInterval(elapsedTimer);
        elapsedTimer = null;
      }
    };
    const finishReady = () => {
      // Drop the cache so the first feature call re-resolves the real port.
      resetSidecarBaseCache();
      stopTimers();
      setPhase("ready");
      setMode("ready");
    };
    const finishFailed = (err: string | null) => {
      stopTimers();
      setLastError(err);
      setPhase("failed");
      setMode("failed");
    };

    async function tick() {
      timer = null;
      if (!alive()) return;
      const elapsed = Date.now() - startedAt;

      const res = await resolveSidecarBase();
      if (!alive()) return;

      if (res.kind === "browser-dev") {
        setDeveloperMode(true);
        setPhase("waiting_for_healthz");
        if (await healthOk(res.base)) {
          if (alive()) finishReady();
          return;
        }
      } else {
        // Tauri window. Read the explicit lifecycle so we can fail fast.
        const snap = await getStartupSnapshot();
        if (!alive()) return;
        if (snap && (snap.state === "failed" || snap.state === "terminated")) {
          finishFailed(snap.lastError);
          return;
        }
        if (res.kind === "tauri") {
          setSidecarPort(res.port);
          setPhase("waiting_for_healthz");
          if (await healthOk(res.base)) {
            if (alive()) finishReady();
            return;
          }
        } else {
          // tauri-starting — port not published yet.
          setPhase("waiting_for_port");
        }
      }

      if (!alive()) return;
      if (elapsed >= FAILURE_BUDGET_MS) {
        finishFailed(null);
        return;
      }
      timer = setTimeout(tick, POLL_INTERVAL_MS);
    }

    tick();

    return () => {
      if (timer !== null) clearTimeout(timer);
      if (elapsedTimer !== null) clearInterval(elapsedTimer);
    };
  }, [runId]);

  const retry = useCallback(() => {
    resetSidecarBaseCache();
    // Best-effort respawn; the polling loop independently observes the new
    // lifecycle. ensure_sidecar may block on the Rust side until healthy/failed,
    // so we never await it here.
    void tauriInvoke<number>("ensure_sidecar");
    setLastError(null);
    setSidecarPort(null);
    setElapsedMs(0);
    setPhase("opening_shell");
    setMode("loading");
    setRunId((n) => n + 1);
  }, []);

  const openLogs = useCallback(async () => {
    await tauriInvoke<string>("open_logs_folder");
  }, []);

  const openLimited = useCallback(() => {
    // Stop the polling loop and drop into the degraded shell.
    runRef.current = -1;
    setPhase("limited");
    setMode("limited");
  }, []);

  const quit = useCallback(async () => {
    await tauriInvoke<void>("quit_app");
  }, []);

  return {
    mode,
    state: { phase, elapsedMs, sidecarPort, developerMode, lastError },
    actions: { retry, openLogs, openLimited, quit },
  };
}
