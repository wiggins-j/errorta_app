// F-DIST-01 slice 5 — first-run activation screen. Shown (full window) when the
// alpha gate is on and the tester hasn't activated yet. On success the parent
// re-polls /alpha/status and the gate clears.

import { useState, type FormEvent } from "react";
import { activateAlpha, AlphaActivationError } from "../../lib/api/alpha";
import { SidecarUnreachableError } from "../../lib/api";
import "./alpha.css";

const ERROR_COPY: Record<string, string> = {
  code_not_found: "That code wasn't recognized. Check for typos and try again.",
  code_exhausted: "This code has already been used.",
  code_disabled: "This code is no longer active. Contact help@errorta.app.",
  code_expired: "This code has expired. Contact help@errorta.app for a new one.",
  device_code_mismatch: "This device is already linked to a different code.",
  license_revoked: "This device's alpha access has ended. Contact help@errorta.app if that seems wrong.",
  offline: "Couldn't reach the activation service. Check your connection and try again.",
};

/** Map any activation failure to honest copy. A transport failure is a
 *  "wait and retry" condition, distinct from a server-side rejection of the
 *  code. Unmapped server codes surface the raw code so a tester report can
 *  identify what happened instead of ending at a generic failure. */
function activationErrorMessage(err: unknown): string {
  if (err instanceof SidecarUnreachableError) {
    return "Errorta is still starting up. Please wait a moment and try again.";
  }
  if (err instanceof AlphaActivationError) {
    return (
      ERROR_COPY[err.code] ??
      `Activation failed (${err.code}). Try again, or contact help@errorta.app.`
    );
  }
  return "Activation failed. Try again, or contact help@errorta.app.";
}

export interface ActivationScreenProps {
  onActivated: () => void;
  /** Injectable for tests; defaults to the real /alpha/activate call. */
  activate?: (code: string) => Promise<unknown>;
}

export default function ActivationScreen({
  onActivated,
  activate = activateAlpha,
}: ActivationScreenProps) {
  const [code, setCode] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const trimmed = code.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await activate(trimmed);
      onActivated();
    } catch (err) {
      // Surface the raw failure for diagnosis (visible in the webview console /
      // logs) — the on-screen copy stays user-friendly.
      console.error("alpha activation failed:", err);
      setError(activationErrorMessage(err));
      setSubmitting(false);
    }
  }

  return (
    <main className="alpha-gate-root">
      <section className="alpha-card" aria-labelledby="alpha-activate-title">
        <h1 id="alpha-activate-title" className="alpha-title">
          Welcome to the Errorta alpha
        </h1>
        <p className="alpha-sub">
          Enter the invite code from your welcome email to unlock the app. Your code links to this
          device.
        </p>
        <form className="alpha-form" onSubmit={onSubmit}>
          <label htmlFor="alpha-code" className="alpha-label">
            Invite code
          </label>
          <input
            id="alpha-code"
            className="alpha-input"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="ERRT-XXXX-XXXX"
            autoComplete="off"
            autoCapitalize="characters"
            spellCheck={false}
            disabled={submitting}
          />
          {error && (
            <p className="alpha-error" role="alert">
              {error}
            </p>
          )}
          <button type="submit" className="alpha-btn" disabled={submitting || !code.trim()}>
            {submitting ? "Activating…" : "Activate"}
          </button>
        </form>
        <p className="alpha-foot">Not a tester yet?</p>
        <a className="alpha-link" href="https://errorta.app" target="_blank" rel="noopener noreferrer">
          Request access at errorta.app
        </a>
      </section>
    </main>
  );
}
