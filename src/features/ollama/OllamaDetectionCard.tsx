import type { OllamaHealth } from "./types";

interface Props {
  health: OllamaHealth | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}

export default function OllamaDetectionCard({ health, loading, error, onRefresh }: Props) {
  const pill = derivePill(health, loading, error);
  return (
    <div className="ollama-status-card">
      <div className="ollama-status-head">
        <span
          className={`ollama-status-dot ollama-status-${pill.tone}`}
          role="img"
          aria-label={pill.label}
        />
        <strong>{pill.label}</strong>
        <button
          type="button"
          className="ollama-recheck"
          onClick={onRefresh}
          disabled={loading}
        >
          {loading ? "Checking…" : "Re-check"}
        </button>
      </div>
      <dl className="ollama-kv">
        <div className="ollama-kv-row">
          <dt>Host</dt>
          <dd>
            <code>{health?.host ?? "—"}</code>
          </dd>
        </div>
        <div className="ollama-kv-row">
          <dt>Version</dt>
          <dd>{health?.version ?? "—"}</dd>
        </div>
        <div className="ollama-kv-row">
          <dt>Managed by Errorta</dt>
          <dd>{health?.managed_by_errorta ? "yes" : "no"}</dd>
        </div>
        {health?.error ? (
          <div className="ollama-kv-row">
            <dt>Last error</dt>
            <dd>
              <code className="ollama-kv-err">{health.error}</code>
            </dd>
          </div>
        ) : null}
        {error ? (
          <div className="ollama-kv-row">
            <dt>Probe error</dt>
            <dd>
              <code className="ollama-kv-err">{error}</code>
            </dd>
          </div>
        ) : null}
      </dl>
    </div>
  );
}

function derivePill(
  health: OllamaHealth | null,
  loading: boolean,
  error: string | null,
): { tone: "ok" | "warn" | "error" | "neutral"; label: string } {
  if (loading && !health) return { tone: "neutral", label: "Checking Ollama…" };
  if (error) return { tone: "error", label: "Backend offline" };
  if (!health) return { tone: "neutral", label: "Unknown" };
  if (health.reachable) return { tone: "ok", label: "Ollama reachable" };
  if (!health.platform_supported)
    return { tone: "warn", label: "Platform not supported for bundled install" };
  return { tone: "warn", label: "Ollama not detected" };
}
