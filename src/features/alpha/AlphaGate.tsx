// F-DIST-01 slice 5 — chooses activation vs lock based on the alpha state. The
// shell renders this full-window (in place of onboarding/shell) whenever
// status.locked is true. Server-side enforcement is the real gate; this is UX.

import type { AlphaStatus } from "../../lib/api/alpha";
import ActivationScreen from "./ActivationScreen";
import LockScreen from "./LockScreen";

export interface AlphaGateProps {
  status: AlphaStatus | null;
  /** Re-poll /alpha/status after activation or a retry. */
  onActivated: () => void;
}

export default function AlphaGate({ status, onActivated }: AlphaGateProps) {
  if (status === null) {
    return (
      <main className="alpha-gate-root">
        <section className="alpha-card" aria-labelledby="alpha-check-title" aria-busy="true">
          <h1 id="alpha-check-title" className="alpha-title">
            Checking alpha access…
          </h1>
          <p className="alpha-sub">Errorta is checking this build before opening the app.</p>
        </section>
      </main>
    );
  }
  if (status.state === "unactivated") {
    return <ActivationScreen onActivated={onActivated} />;
  }
  return <LockScreen status={status} onRetry={onActivated} />;
}
