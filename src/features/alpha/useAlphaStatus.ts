// Polls the local /alpha/status route so the shell can gate on activation/lock
// state. Cheap local call; polled slowly. When disabled (sidecar not up yet) it
// stays idle and reports null.

import { useCallback, useEffect, useState } from "react";
import { getAlphaStatus, type AlphaStatus } from "../../lib/api/alpha";

const POLL_MS = 15000;

export interface UseAlphaStatus {
  status: AlphaStatus | null;
  loading: boolean;
  refresh: () => void;
}

export function useAlphaStatus(enabled: boolean): UseAlphaStatus {
  const [status, setStatus] = useState<AlphaStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [checked, setChecked] = useState(false);
  const [nonce, setNonce] = useState(0);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    if (!enabled) {
      setStatus(null);
      setLoading(false);
      setChecked(false);
      return;
    }
    let cancelled = false;
    let first = true;
    async function poll() {
      if (first) setLoading(true);
      try {
        const s = await getAlphaStatus();
        if (!cancelled) setStatus(s);
      } catch {
        // Sidecar hiccup — keep the last known status; the server-side lock is
        // the real gate, so a transient status-poll failure is cosmetic.
      } finally {
        if (first && !cancelled) {
          setLoading(false);
          setChecked(true);
        }
        first = false;
      }
    }
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [enabled, nonce]);

  // When `enabled` flips true, effects have not run yet. Derive the initial
  // loading state here so App cannot render the shell for one frame before the
  // first status request starts.
  return { status, loading: enabled && !checked ? true : loading, refresh };
}
