// F-DIST-01 slice 6 — the transparency artifact (spec §9). Renders ONLY in
// alpha builds (gate on); production keyless builds show nothing. Honest copy
// about exactly what's collected, an opt-out for the extras, and a "see exactly
// what we send" inspector that shows the pending payload verbatim.

import { useEffect, useState } from "react";
import {
  getTelemetryConsent,
  getTelemetryInspect,
  setTelemetryExtras,
  type AlphaTelemetryInspect,
} from "../../lib/api/alpha";
import { useBackendReady } from "../../lib/backendReady";
import SendFeedback from "./SendFeedback";
import "./alphaTelemetry.css";

export default function AlphaTelemetrySettings() {
  const ready = useBackendReady();
  const [gateEnabled, setGateEnabled] = useState<boolean | null>(null);
  const [extras, setExtras] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showInspect, setShowInspect] = useState(false);
  const [inspect, setInspect] = useState<AlphaTelemetryInspect | null>(null);

  useEffect(() => {
    if (!ready) return; // don't poll a backend that isn't up yet
    let cancelled = false;
    getTelemetryConsent()
      .then((c) => {
        if (cancelled) return;
        setGateEnabled(c.gateEnabled);
        setExtras(c.extrasEnabled);
      })
      .catch(() => {
        if (!cancelled) setGateEnabled(false);
      });
    return () => {
      cancelled = true;
    };
  }, [ready]);

  // Hidden entirely outside the alpha (the gate-off production posture).
  if (gateEnabled !== true) return null;

  async function toggle(next: boolean) {
    setBusy(true);
    setError(null);
    try {
      setExtras(await setTelemetryExtras(next));
    } catch {
      setError("Couldn't update that setting. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function openInspect() {
    setShowInspect(true);
    try {
      setInspect(await getTelemetryInspect());
    } catch {
      // leave the panel empty on a fetch hiccup
    }
  }

  const nothingQueued =
    inspect !== null && inspect.queueLen === 0 && Object.keys(inspect.floor).length === 0;

  return (
    <div className="shell-settings-card alpha-telemetry">
      <h2>Alpha telemetry</h2>
      <p className="alpha-tel-copy">
        While you're in the alpha, Errorta sends us whether your access key is still valid, your app
        version and OS, and counts of how often core features run and how fast. That's it — we never
        see your documents, your questions, your answers, your file names, or your API keys.
      </p>

      <label className="alpha-tel-toggle">
        <input
          type="checkbox"
          checked={extras}
          disabled={busy}
          onChange={(e) => toggle(e.target.checked)}
        />
        <span>Share anonymous usage &amp; performance counts. You can turn this off any time.</span>
      </label>

      <p className="alpha-tel-floor-note">
        The minimal key-and-version check stays on while you're enrolled and goes away entirely when
        Errorta 1.0 ships free to everyone.
      </p>

      {error && (
        <p className="alpha-tel-error" role="alert">
          {error}
        </p>
      )}

      <button
        type="button"
        className="alpha-tel-inspect-btn"
        aria-expanded={showInspect}
        onClick={openInspect}
      >
        See exactly what we send
      </button>

      {showInspect && (
        <div className="alpha-tel-inspect">
          <p className="alpha-tel-inspect-sub">
            Your app version, OS, and an anonymous device id ride every check-in. Beyond that, only
            these counts are queued — nothing else leaves your machine:
          </p>
          <ul className="alpha-tel-list">
            {inspect &&
              Object.entries(inspect.floor).map(([k, v]) => (
                <li key={k}>
                  <code>{k}</code>: {v}
                </li>
              ))}
            {inspect?.queue.map((e, i) => (
              <li key={`q-${i}`}>
                <code>{e.name ?? e.event}</code>
                {e.bucket ? ` [${e.bucket}]` : ""} × {e.count ?? 1}
              </li>
            ))}
            {nothingQueued && <li className="alpha-tel-empty">Nothing queued right now.</li>}
          </ul>
        </div>
      )}

      <div className="alpha-tel-feedback">
        <h3>Report a problem</h3>
        <p className="alpha-tel-floor-note">
          Send us a bug, a crash, or a suggestion. We build a diagnostic bundle with your file
          names, corpus contents, prompts, and keys removed, and show it to you before anything is
          sent.
        </p>
        <SendFeedback />
      </div>
    </div>
  );
}
