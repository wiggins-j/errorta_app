// F006 — Shell pane content. Surfaces shell/sidecar diagnostics and native
// command bridges. User-facing configuration that also exists in Settings
// belongs only in Settings.

import { useEffect, useState } from "react";
import {
  normalizeShellStatus,
  normalizeSidecarPort,
} from "../../lib/api/shell";
import * as shellApi from "../../lib/api/shell";
import { loadTauriInvoke } from "../../lib/sidecarPort";
import type { ShellStatus, SidecarPortInfo } from "./types";
import { ProcessHealthIndicator } from "./ProcessHealthIndicator";
import { DiagnosticsExport } from "./DiagnosticsExport";
import SidecarLifecycleStatus from "./SidecarLifecycleStatus";
import { DataResidencyCard } from "./DataResidencyCard";
import { UpdatesCard } from "./UpdatesCard";
import "./shell.css";

type CoreState =
  | { kind: "loading" }
  | { kind: "ok"; status: ShellStatus; port: SidecarPortInfo }
  | { kind: "error"; message: string };

export function AppShellSettings({ embedded = false }: { embedded?: boolean } = {}) {
  const [state, setState] = useState<CoreState>({ kind: "loading" });
  const [tauriPort, setTauriPort] = useState<{ port: number; source: string; uptime_ms: number } | null>(null);
  const [restartMsg, setRestartMsg] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [status, port] = await Promise.all([
          shellApi.status(),
          shellApi.sidecarPort(),
        ]);
        if (!cancelled) {
          setState({
            kind: "ok",
            status: normalizeShellStatus(status),
            port: normalizeSidecarPort(port),
          });
        }
      } catch (e) {
        if (!cancelled) setState({ kind: "error", message: String(e) });
      }
    }
    load();
    const id = setInterval(load, 7500);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // Try the Tauri-side port command. Falls back silently when not in Tauri.
  useEffect(() => {
    let cancelled = false;
    loadTauriInvoke().then(async (invoke) => {
      if (!invoke || cancelled) return;
      try {
        const v = await invoke<{ port: number; source: string; uptime_ms: number }>(
          "sidecar_port",
        );
        if (!cancelled) setTauriPort(v);
      } catch {
        // command not registered yet in lib.rs — silently fall back to HTTP
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function onRestart() {
    setRestartMsg("requesting…");
    const invoke = await loadTauriInvoke();
    if (!invoke) {
      setRestartMsg("Tauri command bridge unavailable (running in browser).");
      return;
    }
    try {
      const msg = await invoke<string>("restart_sidecar");
      setRestartMsg(msg);
    } catch (e) {
      setRestartMsg(String(e));
    }
  }

  async function onOpenLogs() {
    const invoke = await loadTauriInvoke();
    if (!invoke) {
      setRestartMsg("Logs folder open requires the Tauri shell.");
      return;
    }
    try {
      const path = await invoke<string>("open_logs_folder");
      setRestartMsg(`opened ${path}`);
    } catch (e) {
      setRestartMsg(String(e));
    }
  }

  const cards = (
    <>
        <div className="shell-settings-card">
          <h2>Cold start</h2>
          {state.kind === "ok" ? (
            <p>
              {state.status.sidecar.cold_start_seconds == null
                ? "not measured yet (frontend has not pinged /shell/ready)"
                : `${state.status.sidecar.cold_start_seconds.toFixed(2)} s`}
            </p>
          ) : state.kind === "loading" ? (
            <p>loading…</p>
          ) : (
            <p className="shell-error">{state.message}</p>
          )}
        </div>

        <div className="shell-settings-card">
          <h2>Sidecar port</h2>
          {state.kind === "ok" && (
            <p>
              {state.port.port} <span className="shell-muted">({state.port.source})</span>
            </p>
          )}
          {tauriPort && (
            <p className="shell-muted">
              Tauri: {tauriPort.port} ({tauriPort.source}) · shell uptime{" "}
              {Math.round(tauriPort.uptime_ms / 1000)}s
            </p>
          )}
        </div>

        <div className="shell-settings-card">
          <h2>Data residency</h2>
          <DataResidencyCard />
        </div>

        <div className="shell-settings-card">
          <h2>Updates</h2>
          <UpdatesCard />
        </div>

        <div className="shell-settings-card">
          <h2>Managed processes</h2>
          <ProcessHealthIndicator />
          <div className="shell-actions">
            <button type="button" onClick={onRestart}>Restart sidecar</button>
            <button type="button" onClick={onOpenLogs}>Open logs folder</button>
            {restartMsg && <span className="shell-muted">{restartMsg}</span>}
          </div>
        </div>

        <div className="shell-settings-card">
          <h2>Diagnostics</h2>
          <SidecarLifecycleStatus />
          <DiagnosticsExport />
        </div>
    </>
  );

  // Embedded (inside the Settings tab): the parent group supplies the grid, so
  // just emit the cards. Standalone keeps the page header + grid wrapper.
  if (embedded) return cards;

  return (
    <section className="feature-pane">
      <header className="feature-pane-header">
        <h1>Shell</h1>
      </header>
      <div className="shell-settings-grid">{cards}</div>
    </section>
  );
}

export default AppShellSettings;
