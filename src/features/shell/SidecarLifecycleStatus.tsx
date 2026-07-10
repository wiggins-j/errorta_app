// F048 — sidecar lifecycle status.
//
// Polls /diagnostics/lifecycle, captures the first config signature it sees
// (the "boot" signature), and recommends a restart if a later poll shows a
// different signature — i.e. a restart-relevant setting (residency mode,
// provider keys, Ollama host, …) changed since the sidecar started. The
// recommendation is advisory; restarting is the user's action.

import { useEffect, useRef, useState } from "react";
import {
  getSidecarLifecycle,
  type SidecarLifecycle,
} from "../../lib/api/diagnostics";

const POLL_MS = 5000;

export default function SidecarLifecycleStatus({
  pollMs = POLL_MS,
}: {
  pollMs?: number;
}) {
  const [info, setInfo] = useState<SidecarLifecycle | null>(null);
  const [error, setError] = useState(false);
  const bootSignature = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      try {
        const next = await getSidecarLifecycle();
        if (cancelled) return;
        if (bootSignature.current === null) {
          bootSignature.current = next.config_signature;
        }
        setInfo(next);
        setError(false);
      } catch {
        if (!cancelled) setError(true);
      } finally {
        if (!cancelled) timer = setTimeout(poll, pollMs);
      }
    };
    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [pollMs]);

  if (error && info === null) {
    return (
      <div className="sidecar-lifecycle" role="status">
        <span className="sidecar-lifecycle-dot is-down" aria-hidden="true" />
        <span>Backend offline</span>
      </div>
    );
  }
  if (info === null) {
    return (
      <div className="sidecar-lifecycle" role="status">
        <span>Connecting…</span>
      </div>
    );
  }

  const restartRecommended =
    bootSignature.current !== null &&
    bootSignature.current !== info.config_signature;

  return (
    <div className="sidecar-lifecycle" role="status">
      <div className="sidecar-lifecycle-line">
        <span
          className={`sidecar-lifecycle-dot${
            restartRecommended ? " is-stale" : " is-up"
          }`}
          aria-hidden="true"
        />
        <span>
          Sidecar {restartRecommended ? "running (config changed)" : "running"} ·
          v{info.sidecar_version} · {info.residency_mode}
        </span>
      </div>
      {restartRecommended ? (
        <p className="sidecar-lifecycle-warn">
          A setting changed since the sidecar started — restart Errorta to apply
          it.
        </p>
      ) : null}
      <code className="sidecar-lifecycle-sig" title="config signature">
        {info.config_signature}
      </code>
    </div>
  );
}
