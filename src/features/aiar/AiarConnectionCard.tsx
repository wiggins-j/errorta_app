import { useCallback, useEffect, useState } from "react";
import {
  updateAiarConnection,
  type AiarRuntimeKind,
} from "../../lib/api/aiarConnection";
import { useAiarStatus } from "./useAiarStatus";
import AiarConnectionBadge from "./AiarConnectionBadge";
import "./aiar.css";

export default function AiarConnectionCard() {
  const { status, loading, error, refresh } = useAiarStatus();
  const [mode, setMode] = useState<AiarRuntimeKind>("local-aiar");
  const [displayName, setDisplayName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [token, setToken] = useState("");
  const [timeout, setTimeoutValue] = useState("60");
  const [verifyTls, setVerifyTls] = useState(true);
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const caps = status?.capabilities;

  useEffect(() => {
    if (!status) return;
    setMode(status.runtime_kind);
    setDisplayName(status.display_name === "AIAR disconnected" ? "" : status.display_name);
    setBaseUrl(status.base_url ?? "");
    setTimeoutValue(String(status.timeout_s ?? 60));
    setVerifyTls(status.verify_tls ?? true);
    setToken("");
  }, [status]);

  const save = useCallback(async () => {
    const timeoutValue = Number(timeout);
    const nextMode = mode;
    const trimmedUrl = baseUrl.trim();
    if ((nextMode === "aiar-service" || nextMode === "errorta-sidecar-remote") && !trimmedUrl) {
      setSaveError("Server URL is required.");
      return;
    }
    setBusy(true);
    setSaveError(null);
    setMessage(null);
    try {
      await updateAiarConnection({
        kind: nextMode,
        display_name: displayName.trim() || null,
        base_url: trimmedUrl || null,
        token: token || undefined,
        timeout_s: Number.isFinite(timeoutValue) ? timeoutValue : 60,
        verify_tls: verifyTls,
        allow_disconnected: nextMode === "disconnected",
      });
      setToken("");
      setMessage("AIAR connection saved.");
      await refresh();
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [baseUrl, displayName, mode, refresh, timeout, token, verifyTls]);

  const needsUrl = mode === "aiar-service" || mode === "errorta-sidecar-remote";

  return (
    <div className="aiar-connection-card" data-testid="aiar-connection-card">
      <div className="aiar-connection-row">
        <AiarConnectionBadge status={status} />
        <button type="button" onClick={() => void refresh()} disabled={loading}>
          {loading ? "Checking..." : "Test connection"}
        </button>
      </div>

      {error && <p className="aiar-connection-error">{error}</p>}
      {status?.error_message && (
        <p className="aiar-connection-error">{status.error_message}</p>
      )}
      {saveError && <p className="aiar-connection-error">{saveError}</p>}
      {message && <p className="aiar-connection-message">{message}</p>}

      {status && (
        <dl className="aiar-connection-facts">
          <div>
            <dt>Runtime</dt>
            <dd>{status.runtime_kind}</dd>
          </div>
          <div>
            <dt>Backend</dt>
            <dd>{status.backend_id ?? status.base_url ?? "not resolved"}</dd>
          </div>
          <div>
            <dt>Model</dt>
            <dd>
              {status.active_model ?? "unknown"}
              {status.active_model_ready === false ? " (not ready)" : ""}
            </dd>
          </div>
          <div>
            <dt>Corpora</dt>
            <dd>{status.corpus_count ?? "unknown"}</dd>
          </div>
        </dl>
      )}

      <div className="aiar-connection-form">
        <label>
          Runtime
          <select
            value={mode}
            onChange={(event) => setMode(event.target.value as AiarRuntimeKind)}
            disabled={busy}
            aria-label="AIAR runtime"
          >
            <option value="local-aiar">This Mac</option>
            <option value="aiar-service">AIAR server</option>
            <option value="errorta-sidecar-remote">Remote Errorta sidecar</option>
            <option value="disconnected">Disconnected</option>
          </select>
        </label>
        <label>
          Name
          <input
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            placeholder={mode === "aiar-service" ? "example-host" : "This Mac"}
            disabled={busy || mode === "disconnected"}
          />
        </label>
        {needsUrl ? (
          <>
            <label>
              Server URL
              <input
                value={baseUrl}
                onChange={(event) => setBaseUrl(event.target.value)}
                placeholder={
                  mode === "aiar-service"
                    ? "http://127.0.0.1:8766"
                    : "http://127.0.0.1:8770"
                }
                disabled={busy}
              />
            </label>
            <label>
              Bearer token
              <input
                type="password"
                value={token}
                onChange={(event) => setToken(event.target.value)}
                placeholder={status?.token_configured ? "stored" : ""}
                disabled={busy}
              />
            </label>
            <label>
              Timeout
              <input
                type="number"
                min={1}
                max={600}
                value={timeout}
                onChange={(event) => setTimeoutValue(event.target.value)}
                disabled={busy}
              />
            </label>
            <label className="aiar-connection-check">
              <input
                type="checkbox"
                checked={verifyTls}
                onChange={(event) => setVerifyTls(event.target.checked)}
                disabled={busy}
              />
              Verify TLS
            </label>
          </>
        ) : null}
        <button type="button" onClick={() => void save()} disabled={busy}>
          {busy ? "Saving..." : "Save AIAR connection"}
        </button>
      </div>

      {caps && (
        <div className="aiar-capability-strip" aria-label="AIAR capabilities">
          <span className={caps.answer ? "ok" : "warn"}>answer</span>
          <span className={caps.judge ? "ok" : "warn"}>judge</span>
          <span className={caps.pure_retrieve ? "ok" : "warn"}>retrieve</span>
          <span className={caps.remote_ingest ? "ok" : "warn"}>ingest</span>
        </div>
      )}
    </div>
  );
}
