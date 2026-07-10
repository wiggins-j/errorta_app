import { useCallback, useEffect, useRef, useState } from "react";
import { ollamaApi } from "../../lib/api/index";
import OllamaDetectionCard from "./OllamaDetectionCard";
import OllamaInstallPrompt from "./OllamaInstallPrompt";
import OllamaInstallProgress from "./OllamaInstallProgress";
import OllamaSettingsPanel from "./OllamaSettingsPanel";
import type {
  OllamaHealth,
  OllamaInstallProgress as InstallProgressT,
  OllamaSettings,
} from "./types";
import "./ollama.css";

const POLL_INTERVAL_MS = 1500;

/** When `embedded`, render just the Ollama content (no page header / feature-pane
 * wrapper) so it can slot into the Settings tab as a section. */
export default function OllamaFeature({ embedded = false }: { embedded?: boolean } = {}) {
  const [health, setHealth] = useState<OllamaHealth | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [healthLoading, setHealthLoading] = useState(true);
  const [settings, setSettings] = useState<OllamaSettings | null>(null);
  const [progress, setProgress] = useState<InstallProgressT | null>(null);
  const [skipped, setSkipped] = useState(false);
  const pollRef = useRef<number | null>(null);
  // Guard: only auto-restart a managed-but-down Ollama once per mount, so a
  // genuinely failing managed install doesn't trigger an endless restart loop.
  const autoRestartTriedRef = useRef(false);

  const refreshHealth = useCallback(async () => {
    setHealthLoading(true);
    setHealthError(null);
    try {
      const h = await ollamaApi.health();
      setHealth(h);
    } catch (e) {
      setHealthError(e instanceof Error ? e.message : String(e));
    } finally {
      setHealthLoading(false);
    }
  }, []);

  const refreshSettings = useCallback(async () => {
    try {
      setSettings(await ollamaApi.getSettings());
    } catch {
      // Non-fatal; surfaced via detection card if anything's truly off.
    }
  }, []);

  useEffect(() => {
    void refreshHealth();
    void refreshSettings();
  }, [refreshHealth, refreshSettings]);

  // F003 acceptance: managed Ollama is restarted on next launch after a
  // crash. The sidecar exposes POST /ollama/restart but nothing calls it on
  // its own. When the first health probe comes back unreachable AND the
  // settings say Errorta manages this install, kick a restart once and
  // re-probe.
  useEffect(() => {
    if (!health || !settings) return;
    if (autoRestartTriedRef.current) return;
    if (health.reachable) return;
    if (!health.managed_by_errorta && !settings.managed_by_errorta) return;
    autoRestartTriedRef.current = true;
    (async () => {
      try {
        await ollamaApi.restart();
      } catch {
        // Swallow — the user can still manually install/skip from the UI.
      } finally {
        void refreshHealth();
      }
    })();
  }, [health, settings, refreshHealth]);

  // Poll install progress while an install is in flight.
  useEffect(() => {
    if (!progress) return;
    if (progress.phase === "ready" || progress.phase === "error") {
      // Final state — refresh health/settings once, then stop polling.
      void refreshHealth();
      void refreshSettings();
      return;
    }
    pollRef.current = window.setTimeout(async () => {
      try {
        const p = await ollamaApi.installProgress();
        setProgress(p);
      } catch (e) {
        setProgress((prev) =>
          prev
            ? { ...prev, phase: "error", error: e instanceof Error ? e.message : String(e) }
            : prev,
        );
      }
    }, POLL_INTERVAL_MS);
    return () => {
      if (pollRef.current !== null) window.clearTimeout(pollRef.current);
    };
  }, [progress, refreshHealth, refreshSettings]);

  const onInstall = useCallback(async () => {
    setSkipped(false);
    try {
      const p = await ollamaApi.install();
      setProgress(p);
    } catch (e) {
      setProgress({
        phase: "error",
        percent: 0,
        message: "",
        error: e instanceof Error ? e.message : String(e),
        started_at: null,
        ended_at: null,
        host: null,
        version: null,
      });
    }
  }, []);

  const onSkip = useCallback(() => {
    setSkipped(true);
    // Best-effort: open ollama.com so the user can install manually.
    try {
      window.open("https://ollama.com", "_blank", "noopener");
    } catch {
      // ignore
    }
  }, []);

  const onUpdateHost = useCallback(
    async (host: string) => {
      const next = await ollamaApi.updateSettings({ host });
      setSettings(next);
      void refreshHealth();
    },
    [refreshHealth],
  );

  const onUpdateStorage = useCallback(async (path: string | null) => {
    const next = await ollamaApi.updateSettings({ storage_path: path });
    setSettings(next);
  }, []);

  const showPrompt =
    !skipped &&
    health !== null &&
    !health.reachable &&
    health.needs_install &&
    (progress === null || progress.phase === "idle");

  const installing =
    progress !== null && progress.phase !== "idle" && progress.phase !== "ready" && progress.phase !== "error";

  const body = (
    <>
      <OllamaDetectionCard
        health={health}
        loading={healthLoading}
        error={healthError}
        onRefresh={() => {
          void refreshHealth();
        }}
      />
      {showPrompt ? (
        <OllamaInstallPrompt onInstall={onInstall} onSkip={onSkip} installing={installing} />
      ) : null}
      <OllamaInstallProgress progress={progress} />
      <OllamaSettingsPanel
        settings={settings}
        onUpdateHost={onUpdateHost}
        onUpdateStorage={onUpdateStorage}
      />
    </>
  );

  if (embedded) {
    return <div className="ollama-embed">{body}</div>;
  }

  return (
    <section className="feature-pane">
      <header className="feature-pane-header">
        <h1>Ollama</h1>
      </header>
      {body}
    </section>
  );
}
