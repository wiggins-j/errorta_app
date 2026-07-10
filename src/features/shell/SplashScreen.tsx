// F006 — splash overlay shown during cold-start until the sidecar reports
// healthy + AIAR-available. Stays small on purpose — full lifecycle handling
// happens in the Tauri shell.
import { useEffect, useState } from "react";
import { sidecarHealth } from "../../lib/api";
import * as shellApi from "../../lib/api/shell";

interface Props {
  /** Called once the sidecar is reachable. */
  onReady?: (coldStartSeconds: number | null) => void;
  /** Called if the sidecar fails to come up within the timeout window. */
  onFailure?: () => void;
}

const MAX_ELAPSED_MS = 30_000;
const BACKOFF_MS = [500, 1000, 2000];

export function SplashScreen({ onReady, onFailure }: Props) {
  const [message, setMessage] = useState("Preparing local backend…");
  const [done, setDone] = useState(false);
  const [failed, setFailed] = useState(false);
  const [retryToken, setRetryToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const startedAt = Date.now();

    async function poll() {
      timer = null;
      attempts += 1;
      try {
        await sidecarHealth();
        const cs = await shellApi.markReady().catch(() => null);
        if (cancelled) return;
        setDone(true);
        onReady?.(cs?.cold_start_seconds ?? null);
      } catch {
        if (cancelled) return;
        const elapsed = Date.now() - startedAt;
        if (elapsed >= MAX_ELAPSED_MS) {
          setFailed(true);
          setMessage("Sidecar failed to start. Check logs and retry.");
          onFailure?.();
          return;
        }
        setMessage(
          attempts < 3
            ? "Preparing local backend…"
            : `Still booting sidecar (attempt ${attempts})…`,
        );
        const delay = BACKOFF_MS[Math.min(attempts - 1, BACKOFF_MS.length - 1)];
        timer = setTimeout(poll, delay);
      }
    }
    poll();
    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [onReady, onFailure, retryToken]);

  if (done) return null;
  return (
    <div className="errorta-splash" role="status" aria-live="polite">
      <div className="errorta-splash-logo">Errorta</div>
      <div className="errorta-splash-message">{message}</div>
      {failed && (
        <button
          type="button"
          className="errorta-splash-retry"
          onClick={() => {
            setFailed(false);
            setMessage("Preparing local backend…");
            setRetryToken((n) => n + 1);
          }}
        >
          Retry
        </button>
      )}
    </div>
  );
}

export default SplashScreen;
