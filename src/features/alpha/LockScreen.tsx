// F-DIST-01 slice 5 — lock screen for expired / revoked / build-EOL states.
// Answering is already blocked server-side (403 alpha_locked); this is the
// matching UX. Settings/export/feedback stay reachable from the shell in a
// future slice — here we always surface a way to reach the maintainer.

import type { AlphaStatus } from "../../lib/api/alpha";
import SendFeedback from "./SendFeedback";
import { safeUpdateUrl } from "./safeUpdateUrl";
import "./alpha.css";

export interface LockScreenProps {
  status: AlphaStatus;
  /** Re-poll /alpha/status (used by the "Try again" affordance). */
  onRetry?: () => void;
}

export default function LockScreen({ status, onRetry }: LockScreenProps) {
  const isEol = status.buildEolRequired;
  const isRevoked = status.state === "revoked";
  const updateUrl = safeUpdateUrl(status.updateUrl);

  let title: string;
  let body: string;
  if (isEol) {
    title = "A required update is available";
    body = "This alpha build has been retired. Update to keep using Errorta — your data stays where it is.";
  } else if (isRevoked) {
    title = "Your alpha access has ended";
    body =
      "This device's access to the Errorta alpha has been turned off. If you think that's a mistake, get in touch.";
  } else {
    // expired (grace lapsed while offline)
    title = "Reconnect to renew your alpha access";
    body =
      "Errorta hasn't been able to check your alpha access recently. Connect to the internet once and try again — nothing on your machine has changed.";
  }

  return (
    <main className="alpha-gate-root">
      <section className="alpha-card" aria-labelledby="alpha-lock-title">
        <h1 id="alpha-lock-title" className="alpha-title">
          {title}
        </h1>
        <p className="alpha-sub">{body}</p>
        <div className="alpha-actions">
          {isEol && updateUrl && (
            <a className="alpha-btn" href={updateUrl} target="_blank" rel="noopener noreferrer">
              Get the update
            </a>
          )}
          {!isRevoked && onRetry && (
            <button type="button" className="alpha-btn alpha-btn-secondary" onClick={onRetry}>
              Try again
            </button>
          )}
          <SendFeedback title="Send feedback" />
          <a className="alpha-link" href="mailto:help@errorta.app">
            Contact help@errorta.app
          </a>
        </div>
      </section>
    </main>
  );
}
