import { useEffect, useState } from "react";
import {
  appLooksStale,
  sidecarHealth,
  type SidecarHealth,
} from "../lib/api";

/**
 * Surfaces a clear, dismissible banner when the running app was built before
 * current code (no commit stamp, or missing a capability current builds expose).
 *
 * This exists so build drift announces itself plainly — "your app is old, run
 * scripts/rebuild-app.sh" — instead of manifesting as confusing downstream
 * failures (a feature 404s, or a misleading "sidecar unreachable" banner).
 */
export function StaleBuildBanner() {
  const [health, setHealth] = useState<SidecarHealth | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    sidecarHealth()
      .then((h) => !cancelled && setHealth(h))
      .catch(() => {
        /* transport error is the SidecarStatusBadge's job, not ours */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (dismissed || !appLooksStale(health)) return null;

  const short = health?.build?.commit_short ?? null;

  return (
    <div className="stale-build-banner" role="status">
      <span className="stale-build-banner-text">
        This Errorta app looks out of date
        {short ? ` (built from ${short})` : " (no build stamp)"} — some features
        (e.g. coding-team grounding) need a newer build. Rebuild with{" "}
        <code>bash scripts/rebuild-app.sh --install</code>, then relaunch.
      </span>
      <button
        type="button"
        className="stale-build-banner-dismiss"
        onClick={() => setDismissed(true)}
        aria-label="Dismiss out-of-date notice"
      >
        Dismiss
      </button>
    </div>
  );
}
