import { useCallback, useEffect, useRef, useState } from "react";
import {
  RemoteAiarSettings as RemoteAiarSettingsState,
  getRemoteAiarSettings,
  putRemoteAiarSettings,
  reconnectRemoteAiarTunnel,
} from "../../lib/api/settings";

type Mode = "byo" | "managed";

const TUNNEL_LABEL: Record<string, string> = {
  up: "Connected",
  connecting: "Connecting…",
  reconnecting: "Reconnecting…",
  down: "Down",
  error: "Error",
};

export default function RemoteAiarSettings() {
  const [settings, setSettings] = useState<RemoteAiarSettingsState | null>(null);
  const [mode, setMode] = useState<Mode>("byo");
  const [baseUrl, setBaseUrl] = useState("");
  const [tunnelPort, setTunnelPort] = useState("8766");
  const [sshHost, setSshHost] = useState("");
  const [remotePort, setRemotePort] = useState("8766");
  const [remoteHost, setRemoteHost] = useState("127.0.0.1");
  const [sshPort, setSshPort] = useState("");
  const [sshUser, setSshUser] = useState("");
  const [sshKeyPath, setSshKeyPath] = useState("");
  const [autoStart, setAutoStart] = useState(true);
  const [token, setToken] = useState("");
  const [timeout, setTimeoutValue] = useState("60");
  const [verify, setVerify] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const applySettings = useCallback((next: RemoteAiarSettingsState) => {
    setSettings(next);
    setMode(next.managed ? "managed" : "byo");
    setBaseUrl(next.base_url || "");
    setTunnelPort(next.tunnel_port ? String(next.tunnel_port) : "8766");
    setSshHost(next.ssh_host || "");
    setRemotePort(next.remote_port ? String(next.remote_port) : "8766");
    setRemoteHost(next.remote_host || "127.0.0.1");
    setSshPort(next.ssh_port ? String(next.ssh_port) : "");
    setSshUser(next.ssh_username || "");
    setSshKeyPath(next.ssh_key_path || "");
    setAutoStart(next.auto_start ?? true);
    setTimeoutValue(String(next.timeout_s ?? 60));
    setVerify(next.verify);
    setToken("");
  }, []);

  useEffect(() => {
    let cancelled = false;
    getRemoteAiarSettings()
      .then((next) => {
        if (!cancelled) applySettings(next);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [applySettings]);

  // Poll the tunnel state while managed + not yet settled, so the badge updates
  // as the background watcher brings the tunnel up.
  const settledTunnel =
    settings?.tunnel?.state === "up" || settings?.tunnel?.state === "error";
  const pollRef = useRef(false);
  pollRef.current = mode === "managed" && Boolean(settings?.managed) && !settledTunnel;
  useEffect(() => {
    if (!pollRef.current) return;
    let alive = true;
    const id = setInterval(() => {
      if (!alive || !pollRef.current) return;
      getRemoteAiarSettings()
        .then((next) => alive && setSettings(next))
        .catch(() => undefined);
    }, 3000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [settledTunnel, settings?.managed, mode]);

  const handleSave = useCallback(async () => {
    const timeoutValue = Number(timeout);
    if (!token && !settings?.token_configured) {
      setError("Bearer token is required.");
      return;
    }
    let update;
    if (mode === "managed") {
      const host = sshHost.trim();
      const rport = Number(remotePort);
      if (!host) {
        setError("SSH host (e.g. example-host) is required.");
        return;
      }
      if (!Number.isInteger(rport)) {
        setError("Remote port is required.");
        return;
      }
      update = {
        ssh_host: host,
        remote_host: remoteHost.trim() || "127.0.0.1",
        remote_port: rport,
        ssh_port: sshPort.trim() ? Number(sshPort) : null,
        ssh_username: sshUser.trim() || null,
        ssh_key_path: sshKeyPath.trim() || null,
        auto_start: autoStart,
        token: token || undefined,
        timeout_s: Number.isFinite(timeoutValue) ? timeoutValue : 60,
        verify,
      };
    } else {
      const trimmedUrl = baseUrl.trim();
      const port = Number(tunnelPort);
      if (!trimmedUrl && !Number.isInteger(port)) {
        setError("URL or tunnel port is required.");
        return;
      }
      update = {
        base_url: trimmedUrl,
        tunnel_port: Number.isInteger(port) ? port : null,
        ssh_host: "", // leaving managed mode -> clear the host
        token: token || undefined,
        timeout_s: Number.isFinite(timeoutValue) ? timeoutValue : 60,
        verify,
      };
    }
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      applySettings(await putRemoteAiarSettings(update));
      setMessage("Remote AIAR settings saved.");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [
    applySettings, autoStart, baseUrl, mode, remoteHost, remotePort,
    settings?.token_configured, sshHost, sshKeyPath, sshPort, sshUser,
    timeout, token, tunnelPort, verify,
  ]);

  const handleClear = useCallback(async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      applySettings(await putRemoteAiarSettings({ clear: true }));
      setMessage("Remote AIAR settings cleared.");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [applySettings]);

  const handleReconnect = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      applySettings(await reconnectRemoteAiarTunnel());
      setMessage("Reconnecting the SSH tunnel…");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [applySettings]);

  if (error && !settings) {
    return (
      <div className="provider-keys-settings" role="alert">
        Failed to load remote AIAR settings: {error}
      </div>
    );
  }
  if (!settings) {
    return <div className="provider-keys-settings">Loading…</div>;
  }

  const tunnel = settings.tunnel;

  return (
    <section className="provider-keys-settings" aria-label="Remote AIAR settings">
      <h3>Remote AIAR</h3>
      <div className="provider-key-row" data-testid="remote-aiar-row">
        <div className="provider-key-head">
          <strong>Watchdog endpoint</strong>
          {settings.configured ? (
            <span className="provider-key-status configured">
              configured
              {settings.token_preview ? (
                <>
                  : <code>{settings.token_preview}</code>
                </>
              ) : null}
            </span>
          ) : (
            <span className="provider-key-status missing">not configured</span>
          )}
        </div>

        <div className="provider-key-form">
          <label>
            Connection
            <select
              value={mode}
              onChange={(e) => setMode(e.target.value as Mode)}
              disabled={busy}
              aria-label="Connection mode"
            >
              <option value="byo">Direct URL (bring your own tunnel)</option>
              <option value="managed">Managed SSH tunnel (Errorta connects)</option>
            </select>
          </label>

          {mode === "managed" ? (
            <>
              <label>
                SSH host
                <input
                  value={sshHost}
                  onChange={(e) => setSshHost(e.target.value)}
                  placeholder="example-host (a ~/.ssh/config alias)"
                  disabled={busy}
                  aria-label="SSH host"
                />
              </label>
              <label>
                Remote port
                <input
                  type="number"
                  min={1}
                  max={65535}
                  value={remotePort}
                  onChange={(e) => setRemotePort(e.target.value)}
                  disabled={busy}
                  aria-label="Remote port"
                />
              </label>
              <details>
                <summary>Advanced SSH options</summary>
                <label>
                  Remote host
                  <input
                    value={remoteHost}
                    onChange={(e) => setRemoteHost(e.target.value)}
                    placeholder="127.0.0.1"
                    disabled={busy}
                    aria-label="Remote host"
                  />
                </label>
                <label>
                  SSH port
                  <input
                    type="number"
                    min={1}
                    max={65535}
                    value={sshPort}
                    onChange={(e) => setSshPort(e.target.value)}
                    placeholder="(from ~/.ssh/config)"
                    disabled={busy}
                    aria-label="SSH port"
                  />
                </label>
                <label>
                  SSH user
                  <input
                    value={sshUser}
                    onChange={(e) => setSshUser(e.target.value)}
                    placeholder="(from ~/.ssh/config)"
                    disabled={busy}
                    aria-label="SSH user"
                  />
                </label>
                <label>
                  SSH key path
                  <input
                    value={sshKeyPath}
                    onChange={(e) => setSshKeyPath(e.target.value)}
                    placeholder="(from ~/.ssh/config / ssh-agent)"
                    disabled={busy}
                    aria-label="SSH key path"
                  />
                </label>
              </details>
              <label>
                <input
                  type="checkbox"
                  checked={autoStart}
                  onChange={(e) => setAutoStart(e.target.checked)}
                  disabled={busy}
                />
                Open the tunnel on startup
              </label>
            </>
          ) : (
            <>
              <label>
                Base URL
                <input
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="http://127.0.0.1:8766"
                  disabled={busy}
                  aria-label="Base URL"
                />
              </label>
              <label>
                Tunnel port
                <input
                  type="number"
                  min={1}
                  max={65535}
                  value={tunnelPort}
                  onChange={(e) => setTunnelPort(e.target.value)}
                  disabled={busy}
                  aria-label="Tunnel port"
                />
              </label>
            </>
          )}

          <label>
            Bearer token
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder={settings.token_configured ? "(unchanged)" : "(secret)"}
              autoComplete="off"
              disabled={busy}
            />
          </label>
          <label>
            Timeout seconds
            <input
              type="number"
              min={1}
              max={600}
              value={timeout}
              onChange={(e) => setTimeoutValue(e.target.value)}
              disabled={busy}
            />
          </label>
          <label>
            <input
              type="checkbox"
              checked={verify}
              onChange={(e) => setVerify(e.target.checked)}
              disabled={busy}
            />
            Verify TLS
          </label>
        </div>

        {mode === "managed" && settings.managed && (
          <div className="provider-key-tunnel" data-testid="tunnel-state">
            <span className={`provider-test-badge ${tunnel?.state === "up" ? "ok" : ""}`}>
              Tunnel: {TUNNEL_LABEL[tunnel?.state ?? "down"] ?? "Down"}
              {tunnel?.local_port ? ` (127.0.0.1:${tunnel.local_port})` : ""}
            </span>
            {tunnel?.last_error && tunnel.state !== "up" ? (
              <span className="provider-key-error">{tunnel.last_error}</span>
            ) : null}
            <button onClick={handleReconnect} disabled={busy} type="button">
              Reconnect
            </button>
          </div>
        )}

        <div className="provider-key-actions">
          <button onClick={handleSave} disabled={busy} type="button">
            Save
          </button>
          <button onClick={handleClear} disabled={busy || !settings.configured} type="button">
            Clear
          </button>
        </div>
        {message && <div className="provider-test-badge ok">{message}</div>}
        {error && (
          <div className="provider-key-error" role="alert">
            {error}
          </div>
        )}
      </div>
    </section>
  );
}
