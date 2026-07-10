import { useEffect, useState } from "react";
import {
  approvePairing,
  denyPairing,
  listPairingRequests,
  listTokens,
  revokeToken,
  type PairingSession,
  type ServiceTokenMetadata,
} from "../../lib/api/auth";

type Grant = {
  corpora: string[];
  scopes: string[];
};

function formatDate(value: string | null): string {
  if (!value) return "Never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function toggle(values: string[], value: string, checked: boolean): string[] {
  if (checked) return values.includes(value) ? values : [...values, value];
  return values.filter((item) => item !== value);
}

function defaultGrant(pairing: PairingSession): Grant {
  return {
    corpora: [...pairing.requestedCorpora],
    scopes: [...pairing.requestedScopes],
  };
}

export default function ConnectedAppsSettings() {
  const [pairings, setPairings] = useState<PairingSession[]>([]);
  const [tokens, setTokens] = useState<ServiceTokenMetadata[]>([]);
  const [grants, setGrants] = useState<Record<string, Grant>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setError(null);
    const [nextPairings, nextTokens] = await Promise.all([
      listPairingRequests(),
      listTokens(),
    ]);
    setPairings(nextPairings);
    setTokens(nextTokens);
    setGrants((current) => {
      const next = { ...current };
      nextPairings.forEach((pairing) => {
        if (pairing.status === "pending" && !next[pairing.sessionId]) {
          next[pairing.sessionId] = defaultGrant(pairing);
        }
      });
      return next;
    });
  }

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    load()
      .catch((err) => {
        if (!cancelled) setError(String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function refresh() {
    setLoading(true);
    setMessage(null);
    try {
      await load();
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }

  function setGrant(
    pairing: PairingSession,
    kind: keyof Grant,
    value: string,
    checked: boolean,
  ) {
    setGrants((current) => {
      const grant = current[pairing.sessionId] ?? defaultGrant(pairing);
      return {
        ...current,
        [pairing.sessionId]: {
          ...grant,
          [kind]: toggle(grant[kind], value, checked),
        },
      };
    });
  }

  async function approve(pairing: PairingSession) {
    const grant = grants[pairing.sessionId] ?? defaultGrant(pairing);
    setBusy(pairing.sessionId);
    setError(null);
    setMessage(null);
    try {
      await approvePairing(pairing.sessionId, grant);
      setMessage(`${pairing.appName} connected.`);
      await load();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(null);
    }
  }

  async function deny(pairing: PairingSession) {
    setBusy(pairing.sessionId);
    setError(null);
    setMessage(null);
    try {
      await denyPairing(pairing.sessionId);
      setMessage(`${pairing.appName} denied.`);
      await load();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(null);
    }
  }

  async function revoke(token: ServiceTokenMetadata) {
    setBusy(token.id);
    setError(null);
    setMessage(null);
    try {
      await revokeToken(token.id);
      setMessage(`${token.appName} revoked.`);
      await load();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(null);
    }
  }

  const pending = pairings.filter((item) => item.status === "pending");

  if (loading && pairings.length === 0 && tokens.length === 0 && !error) {
    return <p className="shell-muted">Loading connected apps...</p>;
  }

  return (
    <div className="connected-apps-settings">
      <div className="shell-actions">
        <button type="button" onClick={refresh} disabled={loading || Boolean(busy)}>
          Refresh
        </button>
      </div>

      {message ? <p className="shell-muted">{message}</p> : null}
      {error ? <p className="shell-error">{error}</p> : null}

      <div className="mobile-device-list">
        <h3>Connection requests</h3>
        {pending.length === 0 ? <p className="shell-muted">No pending requests.</p> : null}
        {pending.map((pairing) => {
          const grant = grants[pairing.sessionId] ?? defaultGrant(pairing);
          const disabled = Boolean(busy);
          return (
            <div className="mobile-device-row" key={pairing.sessionId}>
              <div>
                <strong>{pairing.appName}</strong>
                <p className="shell-muted">{pairing.appSlug}</p>
                <p className="shell-muted">Expires {formatDate(pairing.expiresAt)}</p>
              </div>
              <div>
                <div className="mobile-capability-grid">
                  {pairing.requestedCorpora.map((corpus) => (
                    <label key={`${pairing.sessionId}-corpus-${corpus}`}>
                      <input
                        type="checkbox"
                        checked={grant.corpora.includes(corpus)}
                        disabled={disabled}
                        onChange={(event) =>
                          setGrant(pairing, "corpora", corpus, event.currentTarget.checked)
                        }
                      />
                      <span>{corpus}</span>
                    </label>
                  ))}
                  {pairing.requestedScopes.map((scope) => (
                    <label key={`${pairing.sessionId}-scope-${scope}`}>
                      <input
                        type="checkbox"
                        checked={grant.scopes.includes(scope)}
                        disabled={disabled}
                        onChange={(event) =>
                          setGrant(pairing, "scopes", scope, event.currentTarget.checked)
                        }
                      />
                      <span>{scope}</span>
                    </label>
                  ))}
                </div>
              </div>
              <div className="mobile-device-actions">
                <button
                  type="button"
                  onClick={() => approve(pairing)}
                  disabled={disabled || grant.scopes.length === 0}
                >
                  Approve
                </button>
                <button type="button" onClick={() => deny(pairing)} disabled={disabled}>
                  Deny
                </button>
              </div>
            </div>
          );
        })}
      </div>

      <div className="mobile-device-list">
        <h3>Connected apps</h3>
        {tokens.length === 0 ? <p className="shell-muted">No connected apps.</p> : null}
        {tokens.map((token) => (
          <div className="mobile-device-row" key={token.id}>
            <div>
              <strong>{token.appName}</strong>
              <p className="shell-muted">{token.appSlug}</p>
              <p className="shell-muted">Issued {formatDate(token.issuedAt)}</p>
            </div>
            <div>
              <p className="shell-muted">Corpora: {token.corpora.join(", ") || "None"}</p>
              <p className="shell-muted">Scopes: {token.scopes.join(", ") || "None"}</p>
              <p className="shell-muted">Last used {formatDate(token.lastUsedAt)}</p>
            </div>
            <div className="mobile-device-actions">
              <button
                type="button"
                className="mobile-device-delete"
                onClick={() => revoke(token)}
                disabled={Boolean(busy)}
              >
                Revoke
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
