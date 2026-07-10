// F034-10 — Provider keys editor for AppShellSettings.
//
// Renders one section per fixed provider (Anthropic / OpenAI / Google)
// + a custom-entries list. Keys are masked on display; an Edit /
// Save toggle reveals an input. The component never sees a raw key
// after a successful PUT — the server returns masked state.
//
// F040-02 — the individual rows (FixedProviderRow / CustomEntryRow /
// AddCustomForm / SubscriptionCliRow / FixedTestButton + useTestConnection)
// were extracted to the shared `providerRows.tsx` module so the first-run
// "Connect your AI" onboarding step renders the SAME controls with no fork.
import { useCallback, useEffect, useState } from "react";
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
  FixedTestButton,
  SubscriptionCliRow,
} from "./providerRows";

export default function ProviderKeysSettings() {
  const [keys, setKeys] = useState<ProviderKeysMasked | null>(null);
  const [providers, setProviders] = useState<ProviderListItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  const setNormalizedKeys = useCallback((next: ProviderKeysMasked) => {
    setKeys(normalizeProviderKeys(next));
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getProviderKeys(), listGatewayProviders()])
      .then(([k, p]) => {
        if (!cancelled) {
          setNormalizedKeys(k);
          setProviders(p.providers ?? []);
        }
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [setNormalizedKeys]);

  if (error) {
    return (
      <div className="provider-keys-settings" role="alert">
        Failed to load provider keys: {error}
      </div>
    );
  }
  if (!keys) {
    return <div className="provider-keys-settings">Loading…</div>;
  }

  return (
    <section
      className="provider-keys-settings"
      aria-label="Provider API keys"
    >
      <h3>Provider API keys</h3>
      <p className="provider-keys-help">
        Keys are stored locally at{" "}
        <code>~/.errorta/provider-keys.json</code> with mode 0600. They are
        never sent to any server other than the matching provider's API.
      </p>
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
      <div className="provider-keys-divider" />
      <h4>Subscription CLIs</h4>
      <p className="provider-keys-help">
        These use each vendor's installed CLI and account login. Errorta does
        not store their subscription credentials. Assign them in Council rooms
        as <code>claude_cli.*</code>, <code>codex_cli.*</code>, or{" "}
        <code>cursor_cli.*</code>.
      </p>
      {providers
        .filter((p) => p.provider_class.endsWith("_cli"))
        .map((provider) => (
          <SubscriptionCliRow
            key={provider.provider_class}
            provider={provider}
          />
        ))}
      <div className="provider-keys-divider" />
      <div className="provider-key-row" data-testid="provider-row-local">
        <div className="provider-key-head">
          <strong>Local (Ollama)</strong>
          <span className="provider-key-status configured">
            host from <code>Settings → Shell → Ollama host</code>
          </span>
        </div>
        <div className="provider-key-actions">
          <FixedTestButton provider="local" disabled={false} />
        </div>
      </div>
      <div className="provider-keys-divider" />
      <h4>Custom providers</h4>
      <p className="provider-keys-help">
        Point at any OpenAI-compatible or Anthropic-compatible endpoint
        (LM Studio, vLLM, llama.cpp, RunPod, …). Address it in member
        route_ids as <code>custom.&lt;alias&gt;</code>.
      </p>
      {keys.custom.length === 0 ? (
        <p className="provider-keys-empty">No custom providers configured.</p>
      ) : (
        keys.custom.map((entry) => (
          <CustomEntryRow
            key={entry.alias}
            entry={entry}
            onChange={setNormalizedKeys}
          />
        ))
      )}
      <AddCustomForm onChange={setNormalizedKeys} />
    </section>
  );
}
