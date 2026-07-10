// F040-02 — first-run "Connect your AI" onboarding step.
//
// One step that connects ALL of the user's AI in one place:
//   1. Provider API keys (Anthropic / OpenAI / Google + custom endpoints)
//   2. Subscription CLIs (Claude / Codex / Cursor)
//   3. Local AI (Ollama detect + on-demand install)
//
// Everything here is OPTIONAL and deferrable — the step never blocks finishing
// onboarding, and every surface remains editable in Settings. There is NO new
// backend: status comes from the existing gateway/provider-keys/ollama
// endpoints, and "seen" is a localStorage sentinel.
//
// The provider rows are the SAME components Settings renders (extracted to
// `../shell/providerRows`), so there is no logic fork. No token is ever
// rendered — the rows enforce masking.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ProviderKeysMasked,
  ProviderListItem,
  getProviderKeys,
  listGatewayProviders,
  normalizeProviderKeys,
} from "../../lib/api/providerKeys";
import {
  AddCustomForm,
  CustomEntryRow,
  FixedProviderRow,
  SubscriptionCliRow,
} from "../shell/providerRows";
import * as ollamaApi from "../../lib/api/ollama";
import * as hardwareApi from "../../lib/api/hardware";
import type { OllamaHealth } from "../ollama/types";

const SEEN_KEY = "errorta.onboarding.connect-ai.seen";
const SELECTED_MODEL_KEY = "errorta.selectedModel";

// F110 — static size map (~GB on disk) for the recommended tiers. Mirrors
// `install_gb` from errorta_hwdetect/recommendations.json. A model not in the
// map shows no size hint (still pullable). Good enough for alpha (no registry
// probe).
const MODEL_SIZE_GB: Record<string, number> = {
  "qwen2.5:3b": 2,
  "llama3.2:3b": 2,
  "qwen2.5:7b": 5,
  "llama3.1:8b": 5,
  "mistral-small:22b": 13,
  "mistral-small3.1": 14,
  "qwen2.5:32b": 19,
  "llama3.1:70b": 40,
};

function readSelectedModel(): string | null {
  try {
    const v = window.localStorage?.getItem(SELECTED_MODEL_KEY);
    return v && v.trim().length > 0 ? v : null;
  } catch {
    return null;
  }
}

function sizeHint(model: string): string {
  const gb = MODEL_SIZE_GB[model];
  return gb ? ` (~${gb} GB)` : "";
}

function markSeen(): void {
  try {
    window.localStorage?.setItem(SEEN_KEY, "1");
  } catch {
    // localStorage unavailable — the step is optional, so swallow.
  }
}

interface Props {
  onAdvance: () => void;
  onSkip: () => void;
  done?: boolean;
}

// F110 — recommended-model download. Reads errorta.selectedModel; once the
// Ollama runtime is reachable, checks whether the model is installed and offers
// an explicit, sized download with progress. Already-installed → "ready", no
// pull. Never auto-downloads (consented action).
function ModelDownloadSection({ runtimeReachable }: { runtimeReachable: boolean }) {
  const [model, setModel] = useState<string | null>(() => readSelectedModel());
  // Whether we've settled on a model to offer: true immediately if the user
  // already picked one (Settings › Hardware), else after the best-effort
  // recommendation probe below resolves.
  const [recResolved, setRecResolved] = useState<boolean>(
    () => readSelectedModel() != null,
  );
  const [installed, setInstalled] = useState<boolean | null>(null);
  const [checking, setChecking] = useState(false);
  const [pulling, setPulling] = useState(false);
  const [status, setStatus] = useState<string>("");
  const [percent, setPercent] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const stopRef = useRef<(() => void) | null>(null);

  // Abort any in-flight pull stream if the component unmounts.
  useEffect(() => {
    return () => {
      stopRef.current?.();
    };
  }, []);

  // Best-effort recommendation. Onboarding no longer has a Hardware step, so if
  // no model was picked yet, ask the hardware recommender for a compatible
  // default. Any failure is fine — we fall back to a Settings pointer.
  useEffect(() => {
    if (model || recResolved) return;
    let cancelled = false;
    void (async () => {
      try {
        const r = await hardwareApi.report();
        if (!cancelled && r.recommendation?.primary?.compatible) {
          setModel(r.recommendation.primary.id);
        }
      } catch {
        // best-effort; no recommendation available
      } finally {
        if (!cancelled) setRecResolved(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [model, recResolved]);

  const checkInstalled = useCallback(async () => {
    if (!model) return;
    setChecking(true);
    setError(null);
    try {
      const resp = await ollamaApi.getModels(model);
      setInstalled(resp.installed);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setChecking(false);
    }
  }, [model]);

  useEffect(() => {
    if (runtimeReachable && model) void checkInstalled();
  }, [runtimeReachable, model, checkInstalled]);

  const startPull = useCallback(() => {
    if (!model) return;
    setPulling(true);
    setError(null);
    setStatus("Starting download…");
    setPercent(null);
    stopRef.current = ollamaApi.streamPull(model, (e) => {
      if (e.event === "progress") {
        setStatus(e.status);
        setPercent(e.percent);
      } else if (e.event === "done") {
        setStatus(e.message);
        setPercent(100);
        setInstalled(true);
        setPulling(false);
      } else if (e.event === "error") {
        setError(e.error);
        setPulling(false);
      }
    });
  }, [model]);

  if (!model) {
    if (!recResolved) {
      return (
        <p className="provider-keys-help" data-testid="connect-ai-model-checking">
          Checking your hardware for a recommended model…
        </p>
      );
    }
    return (
      <p className="provider-keys-help" data-testid="connect-ai-model-none">
        No model picked yet — choose one in{" "}
        <strong>Settings &rsaquo; Local models</strong>.
      </p>
    );
  }

  if (!runtimeReachable) {
    return (
      <p className="provider-keys-help" data-testid="connect-ai-model-waiting">
        Recommended model: <code>{model}</code>
        {sizeHint(model)}. Install/start Ollama above to download it.
      </p>
    );
  }

  if (installed) {
    return (
      <p
        className="provider-key-status configured"
        data-testid="connect-ai-model-ready"
      >
        <code>{model}</code> is installed and ready.
      </p>
    );
  }

  return (
    <div className="connect-ai-model" data-testid="connect-ai-model-download">
      <p className="provider-keys-help">
        Recommended model <code>{model}</code> isn&rsquo;t downloaded yet.
      </p>
      {pulling ? (
        <div role="status" aria-live="polite">
          <p data-testid="connect-ai-model-progress">
            {status}
            {percent !== null ? ` — ${percent}%` : ""}
          </p>
          <progress max={100} value={percent ?? undefined} />
        </div>
      ) : (
        <button
          type="button"
          onClick={() => void startPull()}
          disabled={checking}
          data-testid="connect-ai-model-pull"
        >
          Download {model}
          {sizeHint(model)}
        </button>
      )}
      {error ? (
        <p className="onboarding-error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

// Local-AI sub-section: reuses the same detect/install logic StepOllama wraps
// (ollamaApi.health / ollamaApi.install). StepOllama has no hardware coupling,
// so the fold-in is a clean inline reuse of the Ollama API path.
function LocalAiSection() {
  const [health, setHealth] = useState<OllamaHealth | null>(null);
  const [installing, setInstalling] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setHealth(await ollamaApi.health());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const install = useCallback(async () => {
    setInstalling(true);
    setError(null);
    try {
      await ollamaApi.install();
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setInstalling(false);
    }
  }, [refresh]);

  return (
    <div className="connect-ai-local">
      <p className="provider-keys-help">
        Ollama runs models locally on this machine. If it's already installed we
        detect it; otherwise we can install it for you.
      </p>
      {health == null ? (
        <p
          className="provider-key-status missing"
          data-testid="connect-ai-ollama-status"
        >
          Detecting…
        </p>
      ) : health.reachable ? (
        <p
          className="provider-key-status configured"
          data-testid="connect-ai-ollama-status"
        >
          ✓ Ollama is running{health.version ? ` (v${health.version})` : ""} at{" "}
          <code>{health.host}</code>
        </p>
      ) : health.platform_supported ? (
        <p
          className="provider-key-status missing"
          data-testid="connect-ai-ollama-status"
        >
          Ollama isn’t running on this machine yet — install it to run models
          locally.
        </p>
      ) : (
        <p
          className="provider-key-status missing"
          data-testid="connect-ai-ollama-status"
        >
          Not reachable at <code>{health.host}</code> — automatic install isn’t
          supported on this platform.
        </p>
      )}
      {error ? (
        <p className="onboarding-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="provider-key-actions">
        {health && !health.reachable && health.platform_supported ? (
          <button
            type="button"
            onClick={() => void install()}
            disabled={installing}
            data-testid="connect-ai-ollama-install"
          >
            {installing ? "Installing…" : "Install Ollama"}
          </button>
        ) : null}
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={installing}
          data-testid="connect-ai-ollama-recheck"
        >
          Re-check
        </button>
      </div>
      <ModelDownloadSection runtimeReachable={health?.reachable === true} />
      <p className="provider-keys-help connect-ai-ollama-pin-note">
        For safety, Errorta installs a version of Ollama we’ve verified rather
        than always pulling the newest build; models are downloaded at their
        latest tag. You can update Ollama yourself any time.
      </p>
    </div>
  );
}

export default function StepConnectAI({ onAdvance, onSkip }: Props) {
  const [keys, setKeys] = useState<ProviderKeysMasked | null>(null);
  const [providers, setProviders] = useState<ProviderListItem[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Bumped by "Scan for CLIs" to remount the CLI rows so each re-detects.
  const [cliScanNonce, setCliScanNonce] = useState(0);

  const setNormalizedKeys = useCallback((next: ProviderKeysMasked) => {
    setKeys(normalizeProviderKeys(next));
  }, []);

  const load = useCallback(async () => {
    try {
      const [k, p] = await Promise.all([
        getProviderKeys(),
        listGatewayProviders(),
      ]);
      setNormalizedKeys(k);
      setProviders(p.providers ?? []);
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }, [setNormalizedKeys]);

  useEffect(() => {
    void load();
  }, [load]);

  // "N connected" summary: configured fixed keys + custom entries + CLIs whose
  // gateway list entry reports configured/connected. Status is read-only; no
  // token text feeds this.
  const fixedConnected = keys
    ? [keys.anthropic, keys.openai, keys.google].filter((s) => s.configured)
        .length
    : 0;
  const customConnected = keys
    ? keys.custom.filter((c) => c.configured).length
    : 0;
  const cliConnected = providers.filter(
    (p) => p.provider_class.endsWith("_cli") && p.connected === true,
  ).length;
  const connectedCount = fixedConnected + customConnected + cliConnected;

  const cliProviders = providers.filter((p) =>
    p.provider_class.endsWith("_cli"),
  );

  const rescanClis = useCallback(() => {
    setCliScanNonce((n) => n + 1);
    void load();
  }, [load]);

  const handleContinue = useCallback(() => {
    markSeen();
    onAdvance();
  }, [onAdvance]);

  const handleSkipStep = useCallback(() => {
    markSeen();
    onAdvance();
  }, [onAdvance]);

  const handleSkipOnboarding = useCallback(() => {
    markSeen();
    onSkip();
  }, [onSkip]);

  return (
    <div className="onboarding-step connect-ai-step">
      <h2>Connect your AI</h2>
      <p>
        Connect any models you want to use — API keys, your Claude / ChatGPT /
        Cursor subscription, or local Ollama. All optional; you can change these
        anytime in <strong>Settings</strong>.
      </p>
      <p
        className="connect-ai-summary"
        data-testid="connect-ai-summary"
        role="status"
      >
        {connectedCount === 0
          ? "No models connected yet."
          : `${connectedCount} connected`}
      </p>
      {loadError ? (
        <p className="onboarding-error" role="alert">
          Failed to load provider status: {loadError}
        </p>
      ) : null}

      <details className="connect-ai-section" open data-testid="connect-ai-keys">
        <summary>
          <span className="connect-ai-section-title">Provider API keys</span>
          <span className="connect-ai-section-status">
            {fixedConnected + customConnected} connected
          </span>
        </summary>
        <p className="provider-keys-help">
          Anthropic, OpenAI, Google, or any OpenAI/Anthropic-compatible
          endpoint. Keys are stored locally at{" "}
          <code>~/.errorta/provider-keys.json</code> (mode 0600) and only sent to
          the matching provider's API.
        </p>
        {keys ? (
          <>
            <FixedProviderRow
              provider="anthropic"
              label="Anthropic (Claude)"
              summary={keys.anthropic}
              onChange={setNormalizedKeys}
            />
            <FixedProviderRow
              provider="openai"
              label="OpenAI (ChatGPT)"
              summary={keys.openai}
              onChange={setNormalizedKeys}
            />
            <FixedProviderRow
              provider="google"
              label="Google (Gemini)"
              summary={keys.google}
              onChange={setNormalizedKeys}
            />
            {keys.custom.length > 0
              ? keys.custom.map((entry) => (
                  <CustomEntryRow
                    key={entry.alias}
                    entry={entry}
                    onChange={setNormalizedKeys}
                  />
                ))
              : null}
            <AddCustomForm onChange={setNormalizedKeys} />
          </>
        ) : (
          <p className="provider-keys-help">Loading…</p>
        )}
      </details>

      <details className="connect-ai-section" data-testid="connect-ai-clis">
        <summary>
          <span className="connect-ai-section-title">Subscription CLIs</span>
          <span className="connect-ai-section-status">
            {cliConnected} connected
          </span>
        </summary>
        <p className="provider-keys-help">
          Use a Claude / ChatGPT / Cursor subscription via each vendor's
          installed CLI. Errorta never stores their credentials — the CLI owns
          the login.
        </p>
        <div className="provider-key-actions">
          <button
            type="button"
            onClick={rescanClis}
            data-testid="connect-ai-scan-clis"
          >
            Scan for CLIs
          </button>
        </div>
        {cliProviders.map((provider) => (
          <SubscriptionCliRow
            key={`${provider.provider_class}:${cliScanNonce}`}
            provider={provider}
          />
        ))}
      </details>

      <details className="connect-ai-section" data-testid="connect-ai-local">
        <summary>
          <span className="connect-ai-section-title">Local AI (Ollama)</span>
        </summary>
        <LocalAiSection />
      </details>

      <p
        className="provider-keys-help connect-ai-settings-note"
        data-testid="connect-ai-settings-note"
      >
        Knowledge, AIAR retrieval, and data residency are set up in{" "}
        <strong>Settings</strong> — see the AIAR guide to learn what AIAR does.
      </p>

      <div className="onboarding-actions">
        <button
          type="button"
          className="onboarding-cta-primary"
          onClick={handleContinue}
          data-testid="connect-ai-continue"
        >
          Continue
        </button>
        <button
          type="button"
          className="onboarding-cta-secondary"
          onClick={handleSkipStep}
          data-testid="connect-ai-skip-step"
        >
          Skip for now
        </button>
        <button
          type="button"
          className="onboarding-cta-link"
          onClick={handleSkipOnboarding}
          data-testid="connect-ai-skip-onboarding"
        >
          Skip onboarding
        </button>
      </div>
    </div>
  );
}
