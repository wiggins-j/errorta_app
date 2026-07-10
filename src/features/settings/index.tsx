// Settings feature — clean panel for user-facing configuration.
//
// Split out from the F006 Shell feature because:
// 1. The Shell pane bundled Tauri command bridges (restart_sidecar,
//    open_logs_folder, sidecar_port) which were crashing the webview
//    in the 2026-06-12 bundle.
// 2. "Shell" is an internal label; users look for "Settings".
//
// This component ONLY uses HTTP to the sidecar (no @tauri-apps/api
// imports), so it survives whatever's wrong in the Shell pane.
//
// What lives here today:
// - Provider keys (F034) — Claude / ChatGPT / Gemini / custom
//   endpoints. The marquee demo gate.
// - Mobile connector (F067) — pairing and LAN bind controls.
// - Tools (F039/F124) — global SearXNG endpoint used by web search rooms.
// - AIAR connection (F116) — canonical AIAR runtime for Judge/Knowledge/Council/Coding.
// - Remote AIAR tunnel (F088/F089) — managed SSH details for AIAR-service hosts.
// - Debug logging (F032) — toggle + Live log tab.
// - Hardware scan + model recommendation (F002).
//
// What's still under Shell:
// - Cold-start timing, sidecar port readout, managed process list,
//   restart sidecar / open logs folder buttons, data residency,
//   updates, sidecar lifecycle, diagnostics export, and Ollama host.
import { useEffect, useState, type ReactNode } from "react";
import "../shell/shell.css";
import ProviderKeysSettings from "../shell/ProviderKeysSettings";
import { DiagnosticsSettings } from "../shell/DiagnosticsSettings";
import MobileConnectorSettings from "../shell/MobileConnectorSettings";
import RemoteAiarSettings from "../shell/RemoteAiarSettings";
import { AppShellSettings } from "../shell/AppShellSettings";
import OllamaFeature from "../ollama/index";
import HardwareFeature from "../hardware/index";
import AiarConnectionCard from "../aiar/AiarConnectionCard";
import { useBackendReady } from "../../lib/backendReady";
import ConnectedAppsSettings from "./ConnectedAppsSettings";
import ModelFamilySettings from "./ModelFamilySettings";
import AlphaTelemetrySettings from "../alpha/AlphaTelemetrySettings";
import {
  getToolsSettings,
  putToolsSettings,
  type ToolsSettings,
} from "../../lib/api/settings";

// F069 — a card whose contents grey out (and stop accepting input) while the
// backend is still booting, instead of hanging or erroring on click.
function SettingsCard({
  title,
  needsBackend = true,
  ready,
  children,
}: {
  title: string;
  needsBackend?: boolean;
  ready: boolean;
  children: ReactNode;
}) {
  const gated = needsBackend && !ready;
  return (
    <div className="shell-settings-card">
      <h2>{title}</h2>
      {gated && (
        <p className="shell-muted settings-card-waiting">
          Available once the local backend is ready…
        </p>
      )}
      <div className={gated ? "settings-card-gated" : undefined} aria-disabled={gated || undefined}>
        {children}
      </div>
    </div>
  );
}

function ToolsSettingsCard() {
  const [settings, setSettings] = useState<ToolsSettings | null>(null);
  const [url, setUrl] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getToolsSettings()
      .then((next) => {
        if (cancelled) return;
        setSettings(next);
        setUrl(next.searxng_url);
      })
      .catch((err) => {
        if (!cancelled) {
          setStatus(err instanceof Error ? err.message : String(err));
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function save(nextUrl = url) {
    setBusy(true);
    setStatus(null);
    try {
      const next = await putToolsSettings({ searxng_url: nextUrl });
      setSettings(next);
      setUrl(next.searxng_url);
      setStatus(next.configured ? "Saved" : "Cleared");
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="settings-tools-card">
      <p className="shell-muted">
        Web-search rooms use this global SearXNG endpoint. Rooms only grant
        search permission.
      </p>
      <label>
        <span>SearXNG endpoint</span>
        <input
          type="url"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="https://searxng.example.com"
          data-testid="settings-searxng-url"
        />
      </label>
      <div className="shell-settings-actions">
        <button type="button" onClick={() => save()} disabled={busy}>
          {busy ? "Saving..." : "Save"}
        </button>
        <button
          type="button"
          onClick={() => save("")}
          disabled={busy || (!url && !settings?.searxng_url)}
        >
          Clear
        </button>
      </div>
      {settings?.env_configured && !settings.searxng_url ? (
        <p className="shell-muted">
          `ERRORTA_SEARXNG_URL` is configured; saving a value here overrides it.
        </p>
      ) : null}
      {status ? <p className="shell-muted" role="status">{status}</p> : null}
    </div>
  );
}

export default function Settings() {
  const ready = useBackendReady();
  return (
    <section className="feature-pane">
      <header className="feature-pane-header">
        <h1>Settings</h1>
        <p className="feature-pane-spec">
          Everything Errorta lets you configure — models, connections, and the
          local system.
        </p>
      </header>

      <div className="settings-group">
        <h2 className="settings-group-heading">AI models &amp; providers</h2>
        <div className="shell-settings-grid">
          <SettingsCard title="Provider keys" ready={ready}>
            <ProviderKeysSettings />
          </SettingsCard>

          <SettingsCard title="Model assignment" ready={ready}>
            <ModelFamilySettings />
          </SettingsCard>

          {/* F134 — Ollama folded in from its own tab. */}
          <SettingsCard title="Local models (Ollama)" ready={ready}>
            <OllamaFeature embedded />
          </SettingsCard>
        </div>
      </div>

      <div className="settings-group">
        <h2 className="settings-group-heading">Knowledge &amp; connections</h2>
        <div className="shell-settings-grid">
          <SettingsCard title="AIAR connection" ready={ready}>
            <AiarConnectionCard />
          </SettingsCard>

          <SettingsCard title="Remote AIAR tunnel" ready={ready}>
            <RemoteAiarSettings />
          </SettingsCard>

          <SettingsCard title="Tools" ready={ready}>
            <ToolsSettingsCard />
          </SettingsCard>

          <SettingsCard title="Mobile connector" ready={ready}>
            <MobileConnectorSettings />
          </SettingsCard>

          <SettingsCard title="Connected apps" ready={ready}>
            <ConnectedAppsSettings />
          </SettingsCard>
        </div>
      </div>

      <div className="settings-group">
        <h2 className="settings-group-heading">System &amp; diagnostics</h2>
        <div className="shell-settings-grid">
          {/* F134 — Shell diagnostics folded in from its own tab (cold start,
              ports, data residency, updates, managed processes, diagnostics). */}
          <AppShellSettings embedded />

          <SettingsCard title="Debug logging" ready={ready}>
            <DiagnosticsSettings />
          </SettingsCard>

          {/* F-DIST-01 — alpha telemetry consent. Self-rendering: shows a card in
              alpha builds (gate on), nothing at all in production. */}
          <AlphaTelemetrySettings />
        </div>
      </div>

      {/* F113: Hardware scan + model recommendation. Renders its own labeled pane. */}
      <div className="settings-group">
        <h2 className="settings-group-heading">Hardware</h2>
        <div className="settings-hardware">
          <HardwareFeature />
        </div>
      </div>
    </section>
  );
}
