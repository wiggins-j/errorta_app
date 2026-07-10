// F-INFRA-12 Phase B Slice 9 — Settings card for data residency.
//
// Three modes:
//   local      → everything on this machine (default).
//   ssh-remote → sidecar runs on a server reachable over SSH; the Rust shell
//                owns a port-forwarded tunnel.
//   cloud      → planned sidecar-over-HTTPS mode; disabled until token auth ships.
//
// Sync ordering between Tauri set_data_residency and PUT /residency:
//   - For ssh-remote: invoke the Tauri command FIRST. It owns the SSH tunnel
//     lifecycle (probe → install → spawn → tunnel → healthz → watcher) AND
//     writes data-residency.json via paths::write_residency on the Rust side.
//     The local Python sidecar lazy-reloads its in-memory ResidencyState on
//     the next GET /residency, so we follow up with PUT /residency to force
//     a same-tick refresh and surface the upstream's view in the response.
  //   - For local: PUT /residency clears the now-irrelevant fields; we follow
  //     with set_data_residency so the Rust shell cancels any tunnel/watcher.
//
// The card never logs `cloud_token` to console or surfaces it in errors. The
// token lives only in component state and is forwarded once on Apply / Test.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getResidency,
  normalizeTunnelState,
  probeResidency,
  putResidency,
  type ResidencyConfig,
  type ResidencyMode,
  type ResidencyProbeResponse,
  type TunnelState,
} from "../../lib/api/residency";
import { TunnelStatusBadge } from "../../components/TunnelStatusBadge";
import { loadTauriInvoke } from "../../lib/sidecarPort";
import "./shell.css";

// Tauri invoke resolves through the shared, bundler-visible `loadTauriInvoke`
// (src/lib/sidecarPort.ts). `null` === browser-dev (no Tauri shell).

// ---------------------------------------------------------------------------
// Wire shapes returned by Tauri commands. These mirror the Rust serializations
// in src-tauri/src/remote_sidecar.rs.
// ---------------------------------------------------------------------------

interface DataResidencyModeReport {
  mode: ResidencyMode;
  ssh_host: string | null;
  remote_sidecar_port: number | null;
  local_tunnel_port: number | null;
  tunnel_state: unknown;
}

interface SshProbeReport {
  uname: string;
  sidecar_present: boolean;
  sidecar_version: string | null;
  raw_stdout: string;
}

const DEFAULT_SSH_PORT = 22;
const DEFAULT_REMOTE_SIDECAR_PORT = 8770;

// ---------------------------------------------------------------------------
// Form state — kept separate from the persisted ResidencyConfig so the user
// can edit fields without committing them until they click Apply.
// ---------------------------------------------------------------------------

interface FormState {
  mode: ResidencyMode;
  ssh_host: string;
  ssh_port: string;
  ssh_key_path: string;
  ssh_username: string;
  remote_sidecar_port: string;
  cloud_url: string;
  cloud_token: string;
}

function formStateFromConfig(cfg: ResidencyConfig): FormState {
  return {
    mode: cfg.mode,
    ssh_host: cfg.ssh_host ?? "",
    ssh_port: String(cfg.ssh_port ?? DEFAULT_SSH_PORT),
    ssh_key_path: cfg.ssh_key_path ?? "",
    ssh_username: cfg.ssh_username ?? "",
    remote_sidecar_port: String(
      cfg.remote_sidecar_port ?? DEFAULT_REMOTE_SIDECAR_PORT,
    ),
    cloud_url: cfg.cloud_url ?? "",
    // cloud_token is never returned by GET — keep the user's typed value if any.
    cloud_token: "",
  };
}

type ApplyStatus =
  | { kind: "idle" }
  | { kind: "applying" }
  | { kind: "ok"; report: DataResidencyModeReport | null }
  | { kind: "error"; message: string };

type TestStatus =
  | { kind: "idle" }
  | { kind: "testing" }
  | { kind: "ssh-ok"; report: SshProbeReport }
  | { kind: "cloud-ok"; result: ResidencyProbeResponse }
  | { kind: "error"; message: string };

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function DataResidencyCard() {
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [persistedMode, setPersistedMode] = useState<ResidencyMode>("local");
  const [tunnel, setTunnel] = useState<TunnelState>({ kind: "down" });
  const [form, setForm] = useState<FormState>({
    mode: "local",
    ssh_host: "",
    ssh_port: String(DEFAULT_SSH_PORT),
    ssh_key_path: "",
    ssh_username: "",
    remote_sidecar_port: String(DEFAULT_REMOTE_SIDECAR_PORT),
    cloud_url: "",
    cloud_token: "",
  });
  const [apply, setApply] = useState<ApplyStatus>({ kind: "idle" });
  const [test, setTest] = useState<TestStatus>({ kind: "idle" });

  // Pull from GET /residency on mount + on window focus. We also poll the
  // Tauri command every 5s while mounted so the badge reflects tunnel events
  // that don't come through a sidecar round trip (re-establish, watchdog).
  const reloadRef = useRef<() => Promise<void>>(async () => {});

  const reload = useCallback(async () => {
    try {
      const r = await getResidency();
      const cfg = r.config;
      setPersistedMode(cfg.mode);
      setTunnel(normalizeTunnelState(r.tunnel_state));
      setForm((prev) =>
        // Preserve any in-flight edits the user made to cloud_token. All other
        // fields snap to the persisted values.
        ({ ...formStateFromConfig(cfg), cloud_token: prev.cloud_token }),
      );
      setLoadError(null);
    } catch (e) {
      setLoadError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  reloadRef.current = reload;

  useEffect(() => {
    reload();
    function onFocus() {
      reload();
    }
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [reload]);

  // Tauri poll for live tunnel state.
  useEffect(() => {
    let cancelled = false;
    let interval: ReturnType<typeof setInterval> | null = null;
    loadTauriInvoke().then((invoke) => {
      if (!invoke || cancelled) return;
      async function poll() {
        try {
          const snap = await invoke!<DataResidencyModeReport>("data_residency_mode");
          if (!cancelled) {
            setTunnel(normalizeTunnelState(snap.tunnel_state));
          }
        } catch {
          // command may not be registered yet — silently skip
        }
      }
      poll();
      interval = setInterval(poll, 5000);
    });
    return () => {
      cancelled = true;
      if (interval) clearInterval(interval);
    };
  }, []);

  // -------------------------------------------------------------------------
  // Field helpers
  // -------------------------------------------------------------------------

  const set = useCallback(
    <K extends keyof FormState>(key: K, value: FormState[K]) => {
      setForm((prev) => ({ ...prev, [key]: value }));
    },
    [],
  );

  const dirty = useMemo(() => form.mode !== persistedMode, [form.mode, persistedMode]);
  // The migration warning shows when the user is switching AWAY from a
  // non-local mode (per spec acceptance criteria: corpora don't travel with
  // the switch). We don't show it when staying on the same mode.
  const showMigrationWarning =
    dirty && persistedMode !== "local" && persistedMode !== form.mode;

  // -------------------------------------------------------------------------
  // Test connection
  // -------------------------------------------------------------------------

  const onTestSsh = useCallback(async () => {
    setTest({ kind: "testing" });
    try {
      const invoke = await loadTauriInvoke();
      if (!invoke) {
        setTest({
          kind: "error",
          message: "SSH test requires the Tauri shell (running in browser-dev).",
        });
        return;
      }
      const port = Number.parseInt(form.ssh_port, 10) || DEFAULT_SSH_PORT;
      const report = await invoke<SshProbeReport>("test_ssh_connection", {
        host: form.ssh_host,
        port,
        keyPath: form.ssh_key_path || null,
        username: form.ssh_username || null,
      });
      setTest({ kind: "ssh-ok", report });
    } catch (e) {
      setTest({ kind: "error", message: String(e) });
      setTunnel({ kind: "error", detail: String(e) });
    }
  }, [form.ssh_host, form.ssh_port, form.ssh_key_path, form.ssh_username]);

  const onTestCloud = useCallback(async () => {
    setTest({ kind: "testing" });
    try {
      const result = await probeResidency(
        form.cloud_url,
        form.cloud_token || undefined,
      );
      if (!result.ok) {
        setTest({
          kind: "error",
          message: result.error ?? "upstream unreachable",
        });
        return;
      }
      setTest({ kind: "cloud-ok", result });
    } catch (e) {
      setTest({ kind: "error", message: String(e) });
    }
  }, [form.cloud_url, form.cloud_token]);

  // -------------------------------------------------------------------------
  // Apply
  // -------------------------------------------------------------------------

  const onApply = useCallback(async () => {
    setApply({ kind: "applying" });
    try {
      const invoke = await loadTauriInvoke();
      // Build the wire payload that BOTH the sidecar PUT body AND the Tauri
      // command consume (the Rust command's `new_state` arg is the same
      // ResidencyState shape).
      const port = Number.parseInt(form.ssh_port, 10);
      const remotePort = Number.parseInt(form.remote_sidecar_port, 10);
      const payload: ResidencyConfig = (() => {
        if (form.mode === "ssh-remote") {
          return {
            mode: "ssh-remote",
            ssh_host: form.ssh_host || null,
            ssh_port: Number.isFinite(port) ? port : DEFAULT_SSH_PORT,
            ssh_key_path: form.ssh_key_path || null,
            ssh_username: form.ssh_username || null,
            remote_sidecar_port: Number.isFinite(remotePort)
              ? remotePort
              : DEFAULT_REMOTE_SIDECAR_PORT,
          };
        }
        if (form.mode === "cloud") {
          throw new Error("Cloud data-residency mode is not enabled yet.");
        }
        return { mode: "local" };
      })();

      // SSH-remote: Tauri first (owns the tunnel bring-up), then PUT to force
      // the local sidecar to re-read its on-disk state in the same tick.
      // Local: PUT first (clears fields), then Tauri so the shell cancels any
      // tunnel/watcher.
      let report: DataResidencyModeReport | null = null;

      if (form.mode === "ssh-remote") {
        if (!invoke) {
          throw new Error(
            "SSH-remote mode requires the Tauri shell (running in browser-dev).",
          );
        }
        report = await invoke<DataResidencyModeReport>("set_data_residency", {
          newState: payload,
        });
        await putResidency({
          ...payload,
          local_tunnel_port: report.local_tunnel_port,
        });
      } else {
        // local / cloud
        await putResidency(payload);
        if (invoke) {
          report = await invoke<DataResidencyModeReport>("set_data_residency", {
            newState: payload,
          });
        }
      }

      setApply({ kind: "ok", report });
      if (report) setTunnel(normalizeTunnelState(report.tunnel_state));
      // Re-fetch GET /residency so the form reflects the canonical persisted
      // shape (the server may have cleared fields on a mode transition).
      await reloadRef.current();
      // Clear the token from the form once it's been applied; the user can
      // re-enter it if they need to re-apply.
      setForm((prev) => ({ ...prev, cloud_token: "" }));
    } catch (e) {
      setApply({ kind: "error", message: String(e) });
    }
  }, [form]);

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  if (loading) {
    return <p className="shell-muted">Loading data residency…</p>;
  }
  if (loadError) {
    return (
      <div role="alert" className="shell-error">
        Could not load residency config: {loadError}
      </div>
    );
  }

  return (
    <div className="data-residency-card" data-testid="data-residency-card">
      <div className="data-residency-header">
        <p className="shell-muted">
          Where your data lives — Local, your own server, or cloud.
        </p>
        <TunnelStatusBadge state={tunnel} />
      </div>

      <fieldset className="data-residency-modes">
        <legend className="data-residency-legend">Mode</legend>
        <label className="data-residency-mode-option">
          <input
            type="radio"
            name="residency-mode"
            value="local"
            checked={form.mode === "local"}
            onChange={() => set("mode", "local")}
            data-testid="residency-mode-local"
          />
          <span>
            <strong>Local</strong>
            <span className="shell-muted"> — this machine</span>
          </span>
        </label>
        <label className="data-residency-mode-option">
          <input
            type="radio"
            name="residency-mode"
            value="ssh-remote"
            checked={form.mode === "ssh-remote"}
            onChange={() => set("mode", "ssh-remote")}
            data-testid="residency-mode-ssh-remote"
          />
          <span>
            <strong>Your server</strong>
            <span className="shell-muted"> — over SSH</span>
          </span>
        </label>
        <label className="data-residency-mode-option">
          <input
            type="radio"
            name="residency-mode"
            value="cloud"
            checked={form.mode === "cloud"}
            disabled
            onChange={() => {}}
            data-testid="residency-mode-cloud"
          />
          <span>
            <strong>Cloud</strong>
            <span className="shell-muted"> — planned, auth not enabled yet</span>
          </span>
        </label>
      </fieldset>

      {form.mode === "ssh-remote" && (
        <div className="data-residency-fields" data-testid="ssh-fields">
          <label>
            SSH host
            <input
              type="text"
              spellCheck={false}
              value={form.ssh_host}
              onChange={(e) => set("ssh_host", e.target.value)}
              placeholder="example-host"
              data-testid="ssh-host"
            />
          </label>
          <label>
            Port
            <input
              type="number"
              min={1}
              max={65535}
              value={form.ssh_port}
              onChange={(e) => set("ssh_port", e.target.value)}
              data-testid="ssh-port"
            />
          </label>
          <label>
            SSH key path
            <input
              type="text"
              spellCheck={false}
              value={form.ssh_key_path}
              onChange={(e) => set("ssh_key_path", e.target.value)}
              placeholder="~/.ssh/id_ed25519"
              data-testid="ssh-key-path"
            />
          </label>
          <label>
            Username <span className="shell-muted">(optional)</span>
            <input
              type="text"
              spellCheck={false}
              value={form.ssh_username}
              onChange={(e) => set("ssh_username", e.target.value)}
              data-testid="ssh-username"
            />
          </label>
          <label>
            Remote sidecar port
            <input
              type="number"
              min={1}
              max={65535}
              value={form.remote_sidecar_port}
              onChange={(e) => set("remote_sidecar_port", e.target.value)}
              data-testid="ssh-remote-port"
            />
          </label>
          <div className="shell-actions">
            <button
              type="button"
              onClick={onTestSsh}
              disabled={!form.ssh_host || test.kind === "testing"}
              data-testid="ssh-test"
            >
              {test.kind === "testing" ? "Testing…" : "Test connection"}
            </button>
          </div>
        </div>
      )}

      {form.mode === "cloud" && (
        <div className="data-residency-fields" data-testid="cloud-fields">
          <label>
            Sidecar URL
            <input
              type="url"
              spellCheck={false}
              value={form.cloud_url}
              onChange={(e) => set("cloud_url", e.target.value)}
              placeholder="https://errorta.example.com"
              data-testid="cloud-url"
            />
          </label>
          <label>
            Access token
            <input
              type="password"
              autoComplete="off"
              value={form.cloud_token}
              onChange={(e) => set("cloud_token", e.target.value)}
              data-testid="cloud-token"
            />
          </label>
          <div className="shell-actions">
            <button
              type="button"
              onClick={onTestCloud}
              disabled={!form.cloud_url || test.kind === "testing"}
              data-testid="cloud-test"
            >
              {test.kind === "testing" ? "Testing…" : "Test connection"}
            </button>
          </div>
        </div>
      )}

      {test.kind === "ssh-ok" && (
        <div
          role="status"
          className="data-residency-toast data-residency-toast-ok"
          data-testid="ssh-test-ok"
        >
          <p>
            <strong>Reachable</strong> — {test.report.uname}
          </p>
          <p className="shell-muted">
            Sidecar:{" "}
            {test.report.sidecar_present
              ? `present (${test.report.sidecar_version ?? "version unknown"})`
              : "not installed — Apply will run install"}
          </p>
        </div>
      )}

      {test.kind === "cloud-ok" && (
        <div
          role="status"
          className="data-residency-toast data-residency-toast-ok"
          data-testid="cloud-test-ok"
        >
          <p>
            <strong>Reachable</strong> — HTTP {test.result.status ?? "?"}
          </p>
        </div>
      )}

      {test.kind === "error" && (
        <div
          role="alert"
          className="data-residency-toast data-residency-toast-err"
          data-testid="test-error"
        >
          {test.message}
        </div>
      )}

      <div className="shell-actions data-residency-apply-row">
        <button
          type="button"
          onClick={onApply}
          disabled={apply.kind === "applying" || form.mode === "cloud"}
          data-testid="residency-apply"
        >
          {apply.kind === "applying" ? "Applying…" : "Apply"}
        </button>
        {apply.kind === "ok" && (
          <span
            className="shell-muted"
            role="status"
            data-testid="residency-apply-ok"
          >
            Applied · mode is now {apply.report?.mode ?? form.mode}
          </span>
        )}
      </div>

      {apply.kind === "error" && (
        <div
          role="alert"
          className="data-residency-toast data-residency-toast-err"
          data-testid="apply-error"
        >
          {apply.message}
        </div>
      )}

      {showMigrationWarning && (
        <p className="data-residency-warning" data-testid="migration-warning">
          Switching modes does <strong>not</strong> copy your corpora. The
          previous sidecar's data stays where it was — use the F010 USB export
          to move a corpus between machines.
        </p>
      )}
    </div>
  );
}

export default DataResidencyCard;
